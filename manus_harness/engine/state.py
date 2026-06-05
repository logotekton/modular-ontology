import os
import re
from enum import Enum
from manus_harness.config import settings

class AgentState(Enum):
    INITIAL = "INITIAL"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    QA_VERIFYING = "QA_VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class StateMachine:
    """
    에이전트 상태 머신 및 todo.md 동기화기 (v1.1.1)
    ----------------------------------
    - 에이전트의 현재 라이프사이클 상태를 제어합니다.
    - 실행 계획(todo.md)을 세션별 격리된 디렉토리에 실시간 영구 보존합니다.
    - todo.md 내의 주석 메타데이터를 파싱하여 서버 재시작 시 상태를 복원합니다.
    """
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.todo_path = settings.get_todo_path(session_id)
        self._current_state = AgentState.INITIAL
        
        # 1. 파일이 존재하는 경우 복구 시도, 없으면 초기화
        if os.path.exists(self.todo_path):
            self.restore_state_from_todo()
        else:
            self.initialize_todo()

    @property
    def current_state(self) -> str:
        return self._current_state.value

    @property
    def current_state_enum(self) -> AgentState:
        return self._current_state

    def transition_to(self, next_state: AgentState):
        print(f"[STATE TRANSITION] {self._current_state.name} -> {next_state.name}")
        self._current_state = next_state
        self.sync_todo_state()

    def initialize_todo(self):
        """
        todo.md 초기 파일 구조를 생성합니다.
        """
        self.write_todo([
            "# Task Plan (todo.md)",
            "",
            "## Goal",
            "- Define your goal here",
            "",
            "## Phases",
            "- [ ] Phase 1: Planning <!-- id: 1 -->",
            "- [ ] Phase 2: Execution <!-- id: 2 -->",
            "- [ ] Phase 3: Verification <!-- id: 3 -->",
            "",
            f"<!-- STATE: {self.current_state} -->"
        ])

    def restore_state_from_todo(self):
        """
        todo.md의 <!-- STATE: ... --> 주석 메타데이터를 파싱하여 내부 상태를 복구합니다. (장애 복구 아키텍처)
        """
        try:
            todo_content = self.read_todo()
            match = re.search(r"<!--\s*STATE:\s*(\w+)\s*-->", todo_content)
            if match:
                state_str = match.group(1)
                if state_str in AgentState.__members__:
                    self._current_state = AgentState[state_str]
                    print(f"[RECOVERY] Successfully restored state to '{self._current_state.name}' from todo.md")
                    return
            print(f"[RECOVERY] No valid state metadata found in todo.md. Defaulting to INITIAL.")
            self._current_state = AgentState.INITIAL
        except Exception as e:
            print(f"[RECOVERY ERROR] Failed to parse state from todo.md: {str(e)}. Defaulting to INITIAL.")
            self._current_state = AgentState.INITIAL

    def read_todo(self) -> str:
        if os.path.exists(self.todo_path):
            with open(self.todo_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def write_todo(self, lines: list):
        with open(self.todo_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def sync_todo_state(self):
        """
        todo.md 파일에 에이전트의 현재 내부 상태를 메타데이터로 기록합니다.
        """
        todo_content = self.read_todo()
        lines = todo_content.splitlines()
        
        # 기존 상태 메타데이터가 있으면 업데이트, 없으면 추가
        state_line_idx = -1
        for idx, line in enumerate(lines):
            if line.startswith("<!-- STATE:"):
                state_line_idx = idx
                break
        
        state_meta = f"<!-- STATE: {self.current_state} -->"
        if state_line_idx != -1:
            lines[state_line_idx] = state_meta
        else:
            lines.append("")
            lines.append(state_meta)
            
        self.write_todo(lines)
