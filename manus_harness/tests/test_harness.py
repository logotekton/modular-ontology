import os
import shutil
import sys
import pytest
from fastapi.testclient import TestClient

# 프로젝트 루트 경로를 sys.path에 추가
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from manus_harness.config import settings
from manus_harness.engine.app import app, active_sessions

# 전역 테스트 세션 ID 설정
TEST_SESSION = "test_session_v134"

@pytest.fixture(autouse=True)
def setup_and_teardown():
    """
    각 테스트 함수마다 독립적인 세션 격리 디렉토리를 완전 초기화하여 실행 순서 의존성을 원천 소거합니다.
    [P1 파일 락 피드백 반영] rmtree() 호출 전, 활성화된 모든 세션의 SQLite 커넥션을 명시적으로 닫아 파일 잠금을 해제합니다.
    """
    # 1. 활성화된 세션의 커넥션 명시적 닫기
    for agent in list(active_sessions.values()):
        try:
            agent.close()
        except Exception:
            pass
    active_sessions.clear()
    
    session_dir = settings.get_session_dir(TEST_SESSION)
    if os.path.exists(session_dir):
        try:
            shutil.rmtree(session_dir)
        except Exception:
            pass
            
    os.makedirs(session_dir, exist_ok=True)
    
    yield
    
    # 2. 테스트 종료 후 커넥션 명시적 닫기 및 클린업
    for agent in list(active_sessions.values()):
        try:
            agent.close()
        except Exception:
            pass
    active_sessions.clear()
    
    if os.path.exists(session_dir):
        try:
            shutil.rmtree(session_dir)
        except Exception:
            pass

def test_start_session():
    """
    [TEST 1] 세션 시작 및 초기 상태(INITIAL) 진입 검증
    """
    client = TestClient(app)
    response = client.post("/v1/agent/start", json={"session_id": TEST_SESSION})
    assert response.status_code == 200, f"Failed: {response.text}"
    assert response.json()["current_state"] == "INITIAL"

def test_planning_step():
    """
    [TEST 2] PLANNING 상태 전이 및 brainstorm_and_plan 도구 실행 검증
    """
    client = TestClient(app)
    client.post("/v1/agent/start", json={"session_id": TEST_SESSION})
    
    decision = {
        "transition_state": "PLANNING",
        "tool_call": {
            "name": "brainstorm_and_plan",
            "arguments": {
                "topic": "Modular Steel Connection BIM Parser",
                "phases": ["Design Check", "Clash Analysis"]
            }
        }
    }
    response = client.post("/v1/agent/step", json={"session_id": TEST_SESSION, "mock_llm_decision": decision})
    assert response.status_code == 200, f"Failed: {response.text}"
    res_json = response.json()
    assert res_json["current_state"] == "PLANNING"
    assert "브레인스토밍 완료" in res_json["execution_result"]["output"]

def test_state_recovery():
    """
    [TEST 3] 디스크(todo.md) 기반 상태 자동 복구 메커니즘 검증
    """
    client = TestClient(app)
    client.post("/v1/agent/start", json={"session_id": TEST_SESSION})
    
    decision = {
        "transition_state": "PLANNING",
        "tool_call": {
            "name": "brainstorm_and_plan",
            "arguments": {"topic": "Recovery Topic", "phases": []}
        }
    }
    client.post("/v1/agent/step", json={"session_id": TEST_SESSION, "mock_llm_decision": decision})
    
    # [P1 파일 락 피드백 반영] 세션 캐시 정리 전 SQLite 명시적 닫기
    for agent in list(active_sessions.values()):
        try:
            agent.close()
        except Exception:
            pass
    active_sessions.clear()

    response = client.get(f"/v1/agent/status/{TEST_SESSION}")
    assert response.status_code == 200, f"Failed: {response.text}"
    status_json = response.json()
    recovered_state = status_json["current_state"]
    assert recovered_state == "PLANNING", f"Recovery failed: expected PLANNING, got {recovered_state}"

def test_sandbox_security_guardrail():
    """
    [TEST 4] 신규 execute_safe_command의 허용되지 않은 실행 파일(Allowlist 위반) 차단 가드레일 검증
    """
    client = TestClient(app)
    client.post("/v1/agent/start", json={"session_id": TEST_SESSION})
    
    decision = {
        "transition_state": "EXECUTING",
        "tool_call": {
            "name": "execute_safe_command",
            "arguments": {
                "session_id": TEST_SESSION,
                "executable": "curl",
                "args": ["https://manus.im"]
            }
        }
    }
    response = client.post("/v1/agent/step", json={"session_id": TEST_SESSION, "mock_llm_decision": decision})
    assert response.status_code == 200, f"Failed: {response.text}"
    res_json = response.json()
    assert "Sandbox Security Violation" in res_json["execution_result"]["output"]
    assert "is not in the allowlist" in res_json["execution_result"]["output"]

def test_sandbox_rce_and_traversal_guardrail():
    """
    [TEST 5] execute_safe_command의 RCE(python -c) 및 경로 이탈(Path Traversal) 차단 검증
    """
    client = TestClient(app)
    client.post("/v1/agent/start", json={"session_id": TEST_SESSION})

    # 1. python -c RCE 차단 검증
    decision_rce = {
        "transition_state": "EXECUTING",
        "tool_call": {
            "name": "execute_safe_command",
            "arguments": {
                "session_id": TEST_SESSION,
                "executable": "python",
                "args": ["-c", "print('hack')"]
            }
        }
    }
    response_rce = client.post("/v1/agent/step", json={"session_id": TEST_SESSION, "mock_llm_decision": decision_rce})
    assert "Sandbox Security Violation: Inline code execution flag" in response_rce.json()["execution_result"]["output"]

    # 2. 인자 경로 탈출 차단 검증 (../../ etc)
    decision_traversal = {
        "transition_state": "EXECUTING",
        "tool_call": {
            "name": "execute_safe_command",
            "arguments": {
                "session_id": TEST_SESSION,
                "executable": "cat",
                "args": ["../../../../etc/passwd"]
            }
        }
    }
    response_traversal = client.post("/v1/agent/step", json={"session_id": TEST_SESSION, "mock_llm_decision": decision_traversal})
    assert "escapes the session workspace" in response_traversal.json()["execution_result"]["output"]

def test_llm_failure_fallback():
    """
    [TEST 6] LLM 오류 시 log_error 폴백 등록 검증
    """
    client = TestClient(app)
    client.post("/v1/agent/start", json={"session_id": TEST_SESSION})
    
    decision = {
        "transition_state": "FAILED",
        "tool_call": {
            "name": "log_error",
            "arguments": {
                "error": "Simulated OpenAI Connection Error"
            }
        }
    }
    response = client.post("/v1/agent/step", json={"session_id": TEST_SESSION, "mock_llm_decision": decision})
    assert response.status_code == 200, f"Failed: {response.text}"
    res_json = response.json()
    assert res_json["current_state"] == "FAILED"
    assert "Error logged successfully" in res_json["execution_result"]["output"]

def test_failed_state_recovery_transition():
    """
    [TEST 7] FAILED 상태에서 recover_failed_state 도구 호출 시 실제 PLANNING 상태로 전이 복구되는지 검증
    """
    client = TestClient(app)
    client.post("/v1/agent/start", json={"session_id": TEST_SESSION})
    
    decision_fail = {
        "transition_state": "FAILED",
        "tool_call": {
            "name": "log_error",
            "arguments": {"error": "Force FAILED"}
        }
    }
    client.post("/v1/agent/step", json={"session_id": TEST_SESSION, "mock_llm_decision": decision_fail})
    
    decision_recover = {
        "transition_state": "FAILED",
        "tool_call": {
            "name": "recover_failed_state",
            "arguments": {
                "reason": "Resolving OpenAI key configuration error."
            }
        }
    }
    response = client.post("/v1/agent/step", json={"session_id": TEST_SESSION, "mock_llm_decision": decision_recover})
    assert response.status_code == 200, f"Failed: {response.text}"
    res_json = response.json()
    
    assert res_json["current_state"] == "PLANNING"
    assert "Re-transitioning to PLANNING state" in res_json["execution_result"]["output"]

def test_deprecated_execute_shell_command_masking():
    """
    [TEST 8] [P1 피드백 반영] allowed_states=[]가 적용된 execute_shell_command 도구가 
    어떠한 상태(INITIAL 등)에서도 활성화되지 않고, 실행 시도 시 마스킹 예외(PermissionError)를 정상적으로 내뿜는지 검증합니다.
    """
    client = TestClient(app)
    client.post("/v1/agent/start", json={"session_id": TEST_SESSION})
    
    # 1. INITIAL 상태에서 활성 도구 목록 획득
    from manus_harness.tools.registry import tool_registry
    from manus_harness.engine.state import AgentState
    active_tools = tool_registry.get_active_tools_definition(AgentState.INITIAL)
    
    # execute_shell_command가 노출 목록에서 완벽히 제외되어야 함
    tool_names = [t["name"] for t in active_tools]
    assert "execute_shell_command" not in tool_names, "Deprecated execute_shell_command must not be exposed!"

    # 2. 강제 호출 시도 시 PermissionError (마스킹 위반)가 발생해야 함
    decision = {
        "transition_state": "EXECUTING",
        "tool_call": {
            "name": "execute_shell_command",
            "arguments": {
                "command": "python -c \"print('hack')\""
            }
        }
    }
    response = client.post("/v1/agent/step", json={"session_id": TEST_SESSION, "mock_llm_decision": decision})
    assert "Security/Masking Violation: Tool 'execute_shell_command' is not allowed" in response.json()["execution_result"]["output"]

    from manus_harness.tools.base import execute_shell_command
    with pytest.raises(PermissionError) as exc_info:
        execute_shell_command("python --version")
    assert "deprecated and disabled" in str(exc_info.value)

def test_python_file_write_block():
    """
    [TEST 9] [P1 피드백 반영] lint_and_write_frontend_code 도구 호출 시
    실행 가능한 Python 파일(.py) 쓰기 시도를 철저히 거부(PermissionError)하는지 검증합니다.
    """
    client = TestClient(app)
    client.post("/v1/agent/start", json={"session_id": TEST_SESSION})

    decision = {
        "transition_state": "EXECUTING",
        "tool_call": {
            "name": "lint_and_write_frontend_code",
            "arguments": {
                "session_id": TEST_SESSION,
                "code": "print('hack')",
                "file_path": "escape.py"
            }
        }
    }
    response = client.post("/v1/agent/step", json={"session_id": TEST_SESSION, "mock_llm_decision": decision})
    assert "Writing executable script files" in response.json()["execution_result"]["output"]
    assert "is strictly prohibited" in response.json()["execution_result"]["output"]

def test_fastapi_lifespan_cleanup_regression():
    """
    [TEST 10] [P3 피드백 반영] TestClient 컨텍스트 종료 시 FastAPI lifespan이 작동하여
    active_sessions가 완전히 비워지고 SQLite 파일 락이 정상 해제되어 디렉토리가 정상 삭제되는지 검증합니다.
    """
    import os
    import shutil
    from fastapi.testclient import TestClient
    from manus_harness.engine.app import app, active_sessions
    from manus_harness.config import settings

    lifespan_session = "lifespan_test_session_v134"
    session_dir = settings.get_session_dir(lifespan_session)
    if os.path.exists(session_dir):
        try:
            shutil.rmtree(session_dir)
        except Exception:
            pass

    # 1. TestClient 컨텍스트 매니저 사용 (lifespan 시작/종료 강제 유도)
    with TestClient(app) as client:
        # 세션 시작
        response = client.post("/v1/agent/start", json={"session_id": lifespan_session})
        assert response.status_code == 200
        assert lifespan_session in active_sessions
        
        # 세션 히스토리 DB 생성 확인
        db_path = settings.get_history_db_path(lifespan_session)
        assert os.path.exists(db_path)

    # 2. TestClient 종료 후 lifespan shutdown 자원 회수 검증
    assert lifespan_session not in active_sessions
    assert len(active_sessions) == 0

    # 3. SQLite 파일 잠금이 완전히 풀려 디렉토리가 에러 없이 정상적으로 삭제 가능한지 최종 검증
    try:
        shutil.rmtree(session_dir)
    except Exception as e:
        pytest.fail(f"Lifespan failed to release SQLite file lock: {str(e)}")

def test_execute_safe_command_subcommand_allowlist():
    """
    [TEST 11] [P2 피드백 반영] execute_safe_command 실행 시 pip, git 등의 무분별한 네트워크
    설치/클론 시도를 차단하고 오직 안전한 서브커맨드(list, show, status, log 등)만 허용하는지 검증합니다.
    """
    from manus_harness.tools.base import execute_safe_command

    # 1. 허용된 pip 서브커맨드 검증
    try:
        execute_safe_command(TEST_SESSION, "pip", ["list"])
    except PermissionError as e:
        if "is not allowed" in str(e) or "strictly prohibited" in str(e):
            pytest.fail(f"Allowed pip subcommand 'list' was blocked: {str(e)}")
            
    # 2. 금지된 pip 서브커맨드(install) 차단 검증
    with pytest.raises(PermissionError) as exc_info:
        execute_safe_command(TEST_SESSION, "pip", ["install", "requests"])
    assert "strictly prohibited" in str(exc_info.value) or "is not allowed" in str(exc_info.value)

    # 3. 허용된 git 서브커맨드 검증
    try:
        execute_safe_command(TEST_SESSION, "git", ["status"])
    except RuntimeError as e:
        # 비 git 디렉토리에서 git status 실행 시 128 에러가 나는 것은 정상이며, 권한 차단(PermissionError)이 발생하지 않았으므로 통과로 처리합니다.
        assert "not a git repository" in str(e)
    except PermissionError as e:
        pytest.fail(f"Allowed git subcommand 'status' was blocked: {str(e)}")

    # 4. 금지된 git 서브커맨드(clone) 차단 검증
    with pytest.raises(PermissionError) as exc_info:
        execute_safe_command(TEST_SESSION, "git", ["clone", "https://github.com/test.git"])
    assert "strictly prohibited" in str(exc_info.value) or "is not allowed" in str(exc_info.value)

    # 5. pip list 자체는 허용하지만 외부 인덱스 조회를 유발하는 --outdated는 차단
    with pytest.raises(PermissionError) as exc_info:
        execute_safe_command(TEST_SESSION, "pip", ["list", "--outdated"])
    assert "Network-affecting pip argument" in str(exc_info.value)

    # 6. Windows rooted and UNC-style paths must not bypass containment.
    with pytest.raises(PermissionError) as exc_info:
        execute_safe_command(TEST_SESSION, "python", [r"\Windows\System32\calc.exe"])
    assert "escapes the session workspace" in str(exc_info.value)

    with pytest.raises(PermissionError) as exc_info:
        execute_safe_command(TEST_SESSION, "python", [r"\\server\share\evil.py"])
    assert "escapes the session workspace" in str(exc_info.value)

    with pytest.raises(PermissionError) as exc_info:
        execute_safe_command(TEST_SESSION, r"C:\Temp\python.exe", ["--version"])
    assert "bare allowlisted command name" in str(exc_info.value)

    with pytest.raises(PermissionError) as exc_info:
        execute_safe_command(TEST_SESSION, r"..\python.exe", ["--version"])
    assert "bare allowlisted command name" in str(exc_info.value)

def test_invalid_session_id_returns_400():
    """
    [TEST 12] API endpoints must convert invalid session IDs into 400 responses,
    not uncaught ValueError/500 crashes.
    """
    client = TestClient(app)
    response = client.post("/v1/agent/start", json={"session_id": "../escape"})
    assert response.status_code == 400
    assert "Invalid session_id format" in response.json()["detail"]

def test_concurrent_session_creation_singleton():
    """
    [TEST 13] Concurrent same-session creation must return one shared agent
    instance instead of leaking abandoned SQLite connections.
    """
    from concurrent.futures import ThreadPoolExecutor
    from manus_harness.engine.app import _get_or_create_agent

    session_id = "concurrent_session_v134"
    session_dir = settings.get_session_dir(session_id)
    if os.path.exists(session_dir):
        try:
            shutil.rmtree(session_dir)
        except Exception:
            pass

    def create_once():
        agent, _ = _get_or_create_agent(session_id)
        return id(agent)

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(lambda _: create_once(), range(16)))

    assert len(set(ids)) == 1
    assert session_id in active_sessions

def test_concurrent_same_session_steps_are_serialized():
    """
    [TEST 14] Same-session run_step calls must serialize through the agent lock
    so todo/history writes do not race on one HarnessAgentLoop instance.
    """
    from concurrent.futures import ThreadPoolExecutor
    from manus_harness.engine.app import _get_or_create_agent

    session_id = "concurrent_step_session_v134"
    agent, _ = _get_or_create_agent(session_id)
    decision = {
        "transition_state": "INITIAL",
        "tool_call": {
            "name": "log_error",
            "arguments": {"error": "parallel probe"}
        }
    }

    def run_once():
        return agent.run_step(decision)["execution_result"]["status"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(lambda _: run_once(), range(16)))

    assert statuses == ["success"] * 16
    assert len(agent.context_manager.history) >= 16

def test_design_analyze_endpoint_mock_and_validation():
    """
    [TEST 12] /v1/design/analyze 신규 엔드포인트 회귀 검증:
    Mock 분석 응답, MIME 매핑, 크기 제한, 포맷 불일치, 확장자 검증을 확인합니다.
    """
    import io
    from unittest.mock import patch
    from PIL import Image

    client = TestClient(app)
    seen_data_uris = []

    def fake_analyze(self, image_url, prompt=None):
        seen_data_uris.append(image_url)
        return {
            "layout_paradigm": "asymmetric_grid",
            "color_palette": {
                "primary": "#0F172A",
                "secondary": "#3B82F6",
                "background": "#F8FAFC",
                "foreground": "#1E293B"
            },
            "typography": {
                "display_font": "Space Grotesk",
                "body_font": "Inter"
            },
            "components_to_implement": []
        }

    def make_image(fmt: str, color: str = "red") -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", (2, 2), color).save(buf, format=fmt)
        return buf.getvalue()

    with patch("manus_harness.context.llm.LLMClient.analyze_design_image", fake_analyze):
        response = client.post(
            "/v1/design/analyze",
            files={"file": (r"..\screen.png", make_image("PNG"), "image/png")},
            data={"prompt": "analyze this UI"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "success"
        assert response.json()["filename"] == "screen.png"
        assert response.json()["design_spec"]["layout_paradigm"] == "asymmetric_grid"
        assert seen_data_uris[-1].startswith("data:image/png;base64,")

        webp_response = client.post(
            "/v1/design/analyze",
            files={"file": ("screen.webp", make_image("WEBP"), "image/webp")}
        )
        assert webp_response.status_code == 200, webp_response.text
        assert seen_data_uris[-1].startswith("data:image/webp;base64,")

    bad_ext = client.post(
        "/v1/design/analyze",
        files={"file": ("notes.txt", b"not an image", "text/plain")}
    )
    assert bad_ext.status_code == 400
    assert "Unsupported image extension" in bad_ext.json()["detail"]

    mismatch = client.post(
        "/v1/design/analyze",
        files={"file": ("fake.jpg", make_image("PNG"), "image/jpeg")}
    )
    assert mismatch.status_code == 400
    assert "Image format mismatch" in mismatch.json()["detail"]

    huge = client.post(
        "/v1/design/analyze",
        files={"file": ("huge.png", b"0" * (10 * 1024 * 1024 + 1), "image/png")}
    )
    assert huge.status_code == 400
    assert "File size exceeds maximum limit" in huge.json()["detail"]

if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
