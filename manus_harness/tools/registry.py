from typing import Dict, Any, Callable, List, Optional
from manus_harness.engine.state import AgentState

class ToolRegistry:
    """
    도구 레지스트리 및 동적 마스킹 (Tool Masking)
    -------------------------------------------
    - 등록된 도구들의 명세 및 실행 함수를 관리합니다.
    - 에이전트의 현재 상태(AgentState)에 따라 실행 및 노출 가능한 도구를 동적으로 제한(Masking)합니다.
    """
    def __init__(self):
        self.tools: Dict[str, Dict[str, Any]] = {}

    def register(self, name: str, description: str, parameters: Dict[str, Any], allowed_states: Optional[List[AgentState]] = None):
        """
        도구를 레지스트리에 등록합니다.
        [P1 피드백 반영] allowed_states가 [] (빈 리스트)인 경우 거짓(Falsy)으로 판단하여 기본 상태로 채우는 버그를 방지하기 위해,
        is not None 체크를 철저히 수행하여 빈 리스트([])가 명확히 마스킹(비활성화) 상태로 보존되도록 개선합니다.
        """
        default_states = [AgentState.INITIAL, AgentState.PLANNING, AgentState.EXECUTING, AgentState.QA_VERIFYING, AgentState.COMPLETED]
        states = allowed_states if allowed_states is not None else default_states

        def decorator(func: Callable):
            self.tools[name] = {
                "name": name,
                "description": description,
                "parameters": parameters,
                "allowed_states": states,
                "func": func
            }
            return func
        return decorator

    def get_active_tools_definition(self, current_state: AgentState) -> List[Dict[str, Any]]:
        """
        현재 에이전트 상태에서 허용된(마스킹되지 않은) 도구 정의 목록만 반환합니다. (KV-Cache 및 환각 방지)
        """
        active_tools = []
        for tool in self.tools.values():
            if current_state in tool["allowed_states"]:
                active_tools.append({
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"]
                })
        return active_tools

    def execute_tool(self, name: str, current_state: AgentState, **kwargs) -> Dict[str, Any]:
        """
        도구를 실행합니다. 상태 권한(State Permission)을 위반할 경우 예외를 발생시킵니다.
        [P2 수정] 도구 함수가 딕셔너리 형태를 반환할 경우, 이를 그대로 루프에 전달하여 상태 전이 등 복합 신호를 투명하게 지원합니다.
        """
        if name not in self.tools:
            raise ValueError(f"Tool '{name}' is not registered.")
        
        tool = self.tools[name]
        if current_state not in tool["allowed_states"]:
            raise PermissionError(
                f"Security/Masking Violation: Tool '{name}' is not allowed in state '{current_state.name}'. "
                f"Allowed states: {[s.name for s in tool['allowed_states']]}"
            )
        
        try:
            result = tool["func"](**kwargs)
            if isinstance(result, dict):
                res_dict = {"status": "success"}
                res_dict.update(result)
                return res_dict
            return {"status": "success", "output": str(result)}
        except Exception as e:
            # 오류 보존 법칙을 위해 에러 발생 시 상세 정보 반환
            import traceback
            tb = traceback.format_exc()
            return {"status": "error", "output": f"Exception: {str(e)}\nTraceback:\n{tb}"}

# 전역 도구 레지스트리 인스턴스
tool_registry = ToolRegistry()
