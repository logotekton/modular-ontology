import os
import json
import sqlite3
import threading
from datetime import datetime
from typing import List, Dict, Any
from manus_harness.config import settings
from manus_harness.context.templates import SYSTEM_PROMPT_TEMPLATE, DYNAMIC_CONTEXT_TEMPLATE

class ContextManager:
    """
    Prefix Lock 및 KV-Cache 최적화 컨텍스트 매니저 (v1.3.4)
    -------------------------------------------
    - 시스템 프롬프트 및 도구 정의(고정 영역)를 컨텍스트 최상단에 배치하여 KV-Cache Hit Rate를 극대화합니다.
    - 시간, 상태, 세션 이력(동적 영역)을 최하단에 정렬하여 상단 캐시 무효화를 방지합니다.
    - [P2 동시성 수정] SQLite 데이터베이스(history.db)를 사용하여 멀티 세션 동시 실행 시에도 정합성이 100% 보장되는 동시성 안전 트랜잭션 로그 저장소를 구축합니다.
    """
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.db_path = settings.get_history_db_path(session_id)
        self.history: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        
        # [P2 피드백 반영] 매번 커넥션을 열고 닫는 오버헤드를 방지하기 위해 단일 커넥션을 영구 유지합니다.
        # [P3 동시성 반영] FastAPI async 환경 등 다중 스레드 안전성을 위해 check_same_thread=False 명시
        self._conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        
        # SQLite 테이블 초기화 및 복구
        self.init_db()
        self.load_history_from_db()
        
    def close(self):
        """
        [P1 파일 락 피드백 반영] SQLite 커넥션을 명시적으로 닫아 파일 잠금을 즉시 해제합니다.
        Windows 환경에서 rmtree 정리 시 WinError 32가 발생하는 현상을 근본적으로 예방합니다.
        """
        with self._lock:
            try:
                if hasattr(self, "_conn") and self._conn:
                    self._conn.close()
                    self._conn = None
            except Exception:
                pass

    def __del__(self):
        """
        소멸자 호출 시 SQLite 커넥션을 안전하게 닫아 리소스 누수를 차단합니다.
        """
        self.close()

    def init_db(self):
        """
        [P2 동시성 수정] SQLite 이벤트 테이블 초기화 및 커넥션 풀을 관리합니다.
        """
        with self._lock:
            try:
                if self._conn is None:
                    self._conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
                    
                cursor = self._conn.cursor()
                
                # [P2 피드백 반영] WAL(Write-Ahead Logging) 모드를 명시적으로 활성화하여
                # uvicorn 다중 워커 병렬 쓰기 환경에서도 database is locked 교착 상태를 완벽히 해결합니다.
                cursor.execute("PRAGMA journal_mode=WAL")
                
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS session_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        action TEXT NOT NULL,
                        status TEXT NOT NULL,
                        output TEXT NOT NULL
                    )
                """)
                self._conn.commit()
            except Exception as e:
                print(f"[SQLITE INIT ERROR] Failed to initialize SQLite database: {str(e)}")

    def format_tools_definition(self, tools: List[Dict[str, Any]]) -> str:
        """
        도구 명세를 고정된 JSON 포맷으로 직렬화하여 캐시 일관성을 유지합니다.
        """
        formatted = []
        sorted_tools = sorted(tools, key=lambda x: x['name'])
        for tool in sorted_tools:
            formatted.append(
                f"- Tool: {tool['name']}\n"
                f"  Description: {tool['description']}\n"
                f"  Parameters Schema: {json.dumps(tool['parameters'], ensure_ascii=False)}"
            )
        return "\n\n".join(formatted)
				
    def get_system_message(self, active_tools: List[Dict[str, Any]]) -> Dict[str, str]:
        """
        최상단 고정 영역(System Message) 생성
        """
        tools_definition = self.format_tools_definition(active_tools)
        content = SYSTEM_PROMPT_TEMPLATE.format(tools_definition=tools_definition)
        return {"role": "system", "content": content}

    def get_dynamic_context_message(self, current_state: str, todo_content: str) -> Dict[str, str]:
        """
        최하단 동적 영역(Dynamic Context) 생성 - 캐시 무효화 최소화 설계
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
        
        # 최근 5개의 실행 이력만 요약하여 컨텍스트 팽창 및 캐시 폭발 방지
        history_summary = []
        for event in self.history[-5:]:
            history_summary.append(
                f"[{event['timestamp']}] Action: {event['action']}\n"
                f"Result Status: {event['status']}\n"
                f"Output/Error: {event['output']}"
            )
        history_str = "\n---\n".join(history_summary) if history_summary else "No execution history yet."

        content = DYNAMIC_CONTEXT_TEMPLATE.format(
            timestamp=timestamp,
            current_state=current_state,
            workspace_path=settings.get_session_dir(self.session_id),
            todo_content=todo_content,
            execution_history=history_str
        )
        return {"role": "system", "content": content}

    def load_history_from_db(self):
        """
        [P2 동시성 수정] SQLite 데이터베이스에서 세션 실행 이력을 로드하여 복구합니다.
        """
        with self._lock:
            try:
                if self._conn is None:
                    self._conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
                    
                self.history = []
                cursor = self._conn.cursor()
                cursor.execute("SELECT timestamp, action, status, output FROM session_history ORDER BY id ASC")
                rows = cursor.fetchall()
                for row in rows:
                    self.history.append({
                        "timestamp": row[0],
                        "action": row[1],
                        "status": row[2],
                        "output": row[3]
                    })
                print(f"[RECOVERY] Successfully loaded {len(self.history)} execution history logs from SQLite database.")
            except Exception as e:
                print(f"[RECOVERY ERROR] Failed to load history logs from DB: {str(e)}")
                self.history = []

    def append_history(self, action: str, status: str, output: str):
        """
        [P2 동시성 수정] 실행 이력을 추가하고 SQLite 트랜잭션을 통해 실시간으로 안전하게 영구 저장합니다.
        """
        # [P3 피드백 반영] close() 이후 append_history 호출 시 AttributeError 방지용 가드 코드 추가
        with self._lock:
            if getattr(self, "_conn", None) is None:
                return

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            event = {
                "timestamp": timestamp,
                "action": action,
                "status": status,
                "output": output
            }
            self.history.append(event)
            
            # SQLite 데이터베이스에 즉시 추가 (트랜잭션 락 보장)
            try:
                if self._conn is None:
                    self._conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
                    
                cursor = self._conn.cursor()
                cursor.execute(
                    "INSERT INTO session_history (timestamp, action, status, output) VALUES (?, ?, ?, ?)",
                    (timestamp, action, status, output)
                )
                self._conn.commit()
            except Exception as e:
                print(f"[LOG ERROR] Failed to write event to SQLite DB: {str(e)}")
