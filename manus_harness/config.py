import os
import platform

class Settings:
    """
    Manus Harness 전역 설정 및 환경 변수 정의 (v1.3.4)
    """
    # 1. OS 환경 판별
    SYSTEM_OS: str = platform.system()  # 'Windows', 'Linux', 'Darwin' 등
    IS_WINDOWS: bool = SYSTEM_OS == "Windows"

    # 2. LLM API 설정 (데모 실행 및 Mock 실행 지원)
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "mock-key-for-harness-demo")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o") # [P1 피드백 반영] 존재하지 않는 gpt-5.5-preview 대신 실제 가용한 gpt-4o로 기본값 복원
    
    # 3. 하네스 캐시 및 임시 파일 저장 경로 (OS 독립적 경로 처리)
    WORKSPACE_DIR: str = os.getenv("MANUS_WORKSPACE", os.path.join(os.path.expanduser("~"), "manus_workspace"))
    
    # 4. 샌드박스 및 API 설정
    # True일 경우, 셸 명령어 실행 시 실제 허용된 화이트리스트(allowlist)만 실행 가능하게 통제합니다.
    SANDBOX_ENABLED: bool = os.getenv("MANUS_SANDBOX_ENABLED", "True").lower() in ("true", "1", "yes")
    PORT: int = int(os.getenv("PORT", "8000"))

    def get_session_dir(self, session_id: str) -> str:
        """
        세션별로 완전히 격리된 데이터 디렉토리 경로를 반환합니다. (멀티세션 안전성 확보)
        [P1 보안 수정] session_id 정규식 slug 검증 및 경로 탈출(Path Traversal) 이중 방어
        """
        import re
        if not re.match(r'^[a-zA-Z0-9_\-]+$', session_id):
            raise ValueError(f"Invalid session_id format: '{session_id}'. Only alphanumeric, underscores, and hyphens are allowed.")
        
        # 1. 상대 경로 결합
        session_dir = os.path.join(self.WORKSPACE_DIR, session_id)
        
        # 2. 절대 경로 정규화 (resolved_path)
        resolved_workspace = os.path.realpath(self.WORKSPACE_DIR)
        resolved_session = os.path.realpath(session_dir)
        
        # 3. 경로 탈출 검증 (상위 디렉토리 탈출 방지)
        sep = os.path.sep
        # Windows의 경우 드라이브 문자 대소문자나 네트워크 경로 처리를 위해 startswith와 sep 확인
        # resolved_session이 resolved_workspace 하위에 속하는지 확인
        prefix = resolved_workspace if resolved_workspace.endswith(sep) else resolved_workspace + sep
        if not resolved_session.startswith(prefix) and resolved_session != resolved_workspace:
            raise ValueError("Path traversal attempt detected. Session directory must reside inside the workspace.")
            
        os.makedirs(resolved_session, exist_ok=True)
        return resolved_session

    def get_todo_path(self, session_id: str) -> str:
        """
        세션별 격리된 todo.md 경로를 반환합니다.
        """
        return os.path.join(self.get_session_dir(session_id), "todo.md")

    def get_history_db_path(self, session_id: str) -> str:
        """
        [P2 수정] 세션별 격리된 SQLite 데이터베이스(history.db) 경로를 반환합니다. (동시성 동기화 지원)
        """
        return os.path.join(self.get_session_dir(session_id), "history.db")

settings = Settings()

# 워크스페이스 루트 디렉토리 생성 보장
os.makedirs(settings.WORKSPACE_DIR, exist_ok=True)
