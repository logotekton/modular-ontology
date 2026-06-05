import time
import threading
from typing import Dict, Any, List
from manus_harness.config import settings
from manus_harness.context.manager import ContextManager
from manus_harness.engine.state import StateMachine, AgentState
from manus_harness.tools.registry import tool_registry

class HarnessAgentLoop:
    """
    Manus형 하네스 에이전트 루프 (v1.3.4)
    --------------------------
    - 상태(State Machine)에 따라 도구를 제한(Tool Masking)합니다.
    - 실행 오류(Keep Error)를 컨텍스트에 보존하여 자가 교정(Self-Correction)을 유도합니다.
    - KV-Cache 최적화 컨텍스트 매니저와 연동됩니다.
    """
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.state_machine = StateMachine(session_id) # 세션 ID를 주입하여 todo.md 및 상태 복원
        self.context_manager = ContextManager(session_id) # 세션 ID를 주입하여 SQLite history.db 복원
        self._run_lock = threading.RLock()
        
    def close(self):
        """
        [P1 파일 락 피드백 반영] 루프 인스턴스 종료 시 SQLite 커넥션을 명시적으로 닫아 파일 잠금을 완벽히 해제합니다.
        """
        try:
            if hasattr(self, "context_manager") and self.context_manager:
                self.context_manager.close()
        except Exception:
            pass

    def __del__(self):
        """
        소멸자 호출 시 안전하게 커넥션을 닫습니다.
        """
        self.close()

    def run_step(self, mock_llm_decision: Dict[str, Any]) -> Dict[str, Any]:
        """
        에이전트 루프의 1단계를 실행합니다.
        
        :param mock_llm_decision: LLM의 결정 시뮬레이션 데이터 (실제 환경에서는 LLM API 호출 결과)
        """
        with self._run_lock:
            return self._run_step_unlocked(mock_llm_decision)

    def _run_step_unlocked(self, mock_llm_decision: Dict[str, Any]) -> Dict[str, Any]:
        print(f"\n--- [AGENT LOOP TURN START] ---")
        current_state_enum = AgentState(self.state_machine.current_state)
        
        # 1. 상태 전이 검사 및 적용
        target_state_str = mock_llm_decision.get("transition_state")
        if target_state_str:
            target_state = AgentState(target_state_str)
            if target_state != current_state_enum:
                self.state_machine.transition_to(target_state)
                current_state_enum = target_state
					
        # 2. 현재 활성화된(마스킹되지 않은) 도구 정의 가져오기 (KV-Cache 최적화 영역)
        active_tools = tool_registry.get_active_tools_definition(current_state_enum)
        system_msg = self.context_manager.get_system_message(active_tools)
        
        # 3. 도구 실행 결정 처리
        tool_call = mock_llm_decision.get("tool_call")
        execution_result = None
        
        if tool_call:
            tool_name = tool_call["name"]
            tool_args = tool_call.get("arguments", {})
            print(f"[TOOL CALL] Invoking '{tool_name}' with args: {tool_args}")
            
            # 도구 실행 (상태 제어 및 예외 처리 내장)
            try:
                execution_result = tool_registry.execute_tool(
                    name=tool_name,
                    current_state=current_state_enum,
                    **tool_args
                )
                
                # [P1 상태 머신 피드백 반영] 도구 결과 내부에 상태 전이 신호가 포함되어 있다면 루프가 수신하여 실제 상태를 전이시킵니다.
                # 1) {"action": "transition", "target": "PLANNING"} 형태 수용
                # 2) {"transition_signal": AgentState.PLANNING} 형태도 함께 수용하여 완벽한 계약 정합성을 유지합니다.
                next_state_str = None
                if isinstance(execution_result, dict):
                    if execution_result.get("action") == "transition":
                        next_state_str = execution_result.get("target")
                    elif execution_result.get("transition_signal"):
                        # [P3 피드백 반영] transition_signal은 과거 v1.3.1 이전의 도구 계약 형태에 대한 방어 코드(하위 호환성용)입니다.
                        # 현재 v1.3.2+ 에서는 "action": "transition" 포맷을 표준으로 사용합니다.
                        sig = execution_result.get("transition_signal")
                        next_state_str = sig.value if isinstance(sig, AgentState) else str(sig)
                
                if next_state_str:
                    next_state = AgentState(next_state_str)
                    print(f"[RECOVERY SIGNAL] Signal detected. Transitioning state from {self.state_machine.current_state} to {next_state_str}")
                    self.state_machine.transition_to(next_state)
                    current_state_enum = next_state
                        
            except Exception as e:
                # 상태 위반 등의 런타임 예외가 발생했을 때, 크래시되지 않고 오류 보존 법칙(Keep Error)을 적용합니다.
                import traceback
                tb = traceback.format_exc()
                execution_result = {
                    "status": "error",
                    "output": f"Harness Engine Blocked: {str(e)}\nTraceback:\n{tb}"
                }
            
            # 4. 오류 보존 법칙 (Keep Error): 에러 발생 여부와 상관없이 컨텍스트에 결과 누적
            if isinstance(execution_result, dict):
                status = execution_result.get("status", "success")
                output = execution_result.get("output", str(execution_result))
            else:
                status = "success"
                output = str(execution_result)
            
            if status == "error":
                print(f"[KEEP ERROR] Execution failed, preserving traceback in context.")
            else:
                print(f"[TOOL SUCCESS] Execution completed successfully.")
                
            self.context_manager.append_history(
                action=f"Call {tool_name} with {tool_args}",
                status=status,
                output=output
            )
					
        # 5. 동적 컨텍스트 업데이트 (최하단 동적 영역 생성)
        todo_content = self.state_machine.read_todo()
        dynamic_msg = self.context_manager.get_dynamic_context_message(
            current_state=self.state_machine.current_state,
            todo_content=todo_content
        )
					
        # 6. 최종 LLM 전달용 풀 컨텍스트 패키징 (KV-Cache 최적화 구조)
        full_context = [
            system_msg,         # 최상단 고정 (System Prompt + Masked Tools) -> Cache Lock!
            dynamic_msg         # 최하단 동적 (Timestamp, State, Todo, History) -> Variable Input
        ]
					
        return {
            "current_state": self.state_machine.current_state,
            "execution_result": execution_result,
            "full_context": full_context
        }
