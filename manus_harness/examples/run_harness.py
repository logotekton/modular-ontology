import os
import sys
import shutil

# 패키지 경로 추가
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from manus_harness.config import settings
from manus_harness.engine.loop import HarnessAgentLoop
from manus_harness.engine.state import AgentState
from manus_harness.tools import base  # 도구 등록 활성화

def run_demo():
    print("==================================================")
    print("  Manus Harness Core Engine Demo Run (v1.3.4)")
    print("  Platform Independent & Cross-OS Compatible")
    print("==================================================")

    session_id = "session_modular_01"
    
    # 0. 기존 세션 디렉토리가 있다면 깨끗하게 정리하여 항상 신규 세션 상태로 시작
    session_dir = settings.get_session_dir(session_id)
    if os.path.exists(session_dir):
        try:
            shutil.rmtree(session_dir)
        except Exception:
            pass
            
    # 1. 하네스 에이전트 루프 초기화 (SQLite 동시성 및 세션 격리 디렉토리 자동 생성)
    agent = HarnessAgentLoop(session_id=session_id)
    resolved_session_dir = os.path.realpath(session_dir)
    
    # [P2 피드백 반영 및 v1.3.4 가드레일 준수]
    # 1) lint_and_write_frontend_code는 오직 프론트엔드(.tsx, .html 등) 파일만 작성할 수 있으며 .py 쓰기는 금지됩니다.
    # 2) 데모 내에서 Python 스크립트(bim_sim.py 등)를 작성할 때는 일반 파일 쓰기 기법(파이썬 open)을 사용합니다.
    # 3) run_qa_verification 및 execute_safe_command 호출 시 새 시그니처 사양인 session_id를 정확하게 전달합니다.
    # 4) 각 단계별 성공/실패 여부를 엄격하게 assert하여, 오류 발생 시 "Demo Run Completed Successfully"를 무작정 출력하지 않고 즉시 중단되도록 개정합니다.
    
    # 세션 디렉토리 내에 안전하게 파이썬 스크립트 작성 (도구 우회 RCE가 아닌 호스트 레벨에서의 안전한 파일 사전 주입)
    sim_script_path = os.path.join(resolved_session_dir, "bim_sim.py")
    with open(sim_script_path, "w", encoding="utf-8") as f:
        f.write("""# BIM Parser Simulator
import sys
print("BIM Connection Analysis: 42 Steel Columns Verified.")
sys.exit(0)
""")

    fail_script_path = os.path.join(resolved_session_dir, "bim_fail.py")
    with open(fail_script_path, "w", encoding="utf-8") as f:
        f.write("""# BIM Parser Failure Simulator
raise FileNotFoundError("BIM Steel connection database is corrupted!")
""")

    # 2. 시나리오 정의: 정상 흐름, 도구 마스킹, 보안 가드레일, 오류 제어 검증
    turns = [
        # Turn 1: PLANNING 상태로 진입하고 기획 수립 도구 호출 (정상)
        {
            "transition_state": "PLANNING",
            "tool_call": {
                "name": "brainstorm_and_plan",
                "arguments": {
                    "topic": "Modular BIM Parser",
                    "phases": ["Design", "Build", "QA"]
                }
            }
        },
        # Turn 2: PLANNING 상태에서 EXECUTING 전용 도구(execute_safe_command) 호출 시도 (마스킹 위반 예외 발생 검증)
        {
            "transition_state": "PLANNING",
            "tool_call": {
                "name": "execute_safe_command",
                "arguments": {
                    "session_id": session_id,
                    "executable": "python",
                    "args": ["--version"]
                }
            }
        },
        # Turn 3: EXECUTING 상태로 정상 전환 후, lint_and_write_frontend_code 도구로 세션 내부 프론트엔드 코드 파일 작성 (안전 디자인 검증 및 자동 교정)
        {
            "transition_state": "EXECUTING",
            "tool_call": {
                "name": "lint_and_write_frontend_code",
                "arguments": {
                    "session_id": session_id,
                    "code": """
                    export default function BimUI() {
                        return <div className="h-[400px] bg-card text-card-foreground">BIM Visualizer</div>;
                    }
                    """,
                    "file_path": "BimUI.tsx"
                }
            }
        },
        # Turn 4: EXECUTING 상태에서, 세션 내부 정상 스크립트 파일을 execute_safe_command로 실행 (안전 실행 검증)
        {
            "transition_state": "EXECUTING",
            "tool_call": {
                "name": "execute_safe_command",
                "arguments": {
                    "session_id": session_id,
                    "executable": "python",
                    "args": ["bim_sim.py"]
                }
            }
        },
        # Turn 5: EXECUTING 상태에서 오류 발생 스크립트 실행 (오류 보존 법칙 검증 - Traceback 보존)
        {
            "transition_state": "EXECUTING",
            "tool_call": {
                "name": "execute_safe_command",
                "arguments": {
                    "session_id": session_id,
                    "executable": "python",
                    "args": ["bim_fail.py"]
                }
            }
        },
        # Turn 6: QA_VERIFYING 상태로 전환 후 빌드 검증 수행 (정상)
        {
            "transition_state": "QA_VERIFYING",
            "tool_call": {
                "name": "run_qa_verification",
                "arguments": {
                    "session_id": session_id,
                    "target_path": "BimUI.tsx"
                }
            }
        }
    ]

    # 시나리오 실행 루프 및 검증 단언(Assert)
    try:
        # Turn 1 검증 (도구 실행 결과는 항상 status, output을 갖는 dict 구조로 래핑됨)
        print("\n[TURN 1] Executing Brainstorm & Plan (PLANNING State)...")
        res1 = agent.run_step(turns[0])
        exec_res1 = res1["execution_result"]
        assert isinstance(exec_res1, dict)
        assert exec_res1.get("status") == "success"
        assert "브레인스토밍 완료" in exec_res1.get("output", "")
        print(f"Output: {exec_res1['output']}")

        # Turn 2 검증 (마스킹 예외로 인해 {"status": "error"} 반환)
        print("\n[TURN 2] Testing Masking Violation (PLANNING -> execute_safe_command)...")
        res2 = agent.run_step(turns[1])
        exec_res2 = res2["execution_result"]
        assert isinstance(exec_res2, dict)
        assert exec_res2.get("status") == "error"
        assert "Security/Masking Violation" in exec_res2.get("output", "")
        print("Success: Security/Masking Violation caught correctly.")

        # Turn 3 검증 (정상 프론트엔드 파일 작성 및 자동 교정 검증)
        print("\n[TURN 3] Writing Frontend Component with Linter (EXECUTING State)...")
        res3 = agent.run_step(turns[2])
        exec_res3 = res3["execution_result"]
        assert isinstance(exec_res3, dict)
        assert exec_res3.get("status") == "success"
        assert "written successfully" in exec_res3.get("output", "")
        print(f"Output: {exec_res3['output']}")
        
        # 파일이 생성되었고, h-[400px]가 min-h-[400px]로 자동 자가 교정되었는지 확인
        written_path = os.path.join(resolved_session_dir, "BimUI.tsx")
        assert os.path.exists(written_path)
        with open(written_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "min-h-[400px]" in content
        print("Success: Frontend code written and safe design auto-enforced.")

        # Turn 4 검증 (정상 스크립트 안전 실행)
        print("\n[TURN 4] Executing Safe Command (BIM Simulator)...")
        res4 = agent.run_step(turns[3])
        exec_res4 = res4["execution_result"]
        assert isinstance(exec_res4, dict)
        assert exec_res4.get("status") == "success"
        assert "42 Steel Columns Verified" in exec_res4.get("output", "")
        print(f"Output: {exec_res4['output'].strip()}")

        # Turn 5 검증 (오류 발생 스크립트 실행 - 예외 발생으로 {"status": "error"} 반환)
        print("\n[TURN 5] Executing Unsafe Command with Failure (BIM Failure Simulator)...")
        res5 = agent.run_step(turns[4])
        exec_res5 = res5["execution_result"]
        assert isinstance(exec_res5, dict)
        assert exec_res5.get("status") == "error"
        assert "BIM Steel connection database is corrupted" in exec_res5.get("output", "")
        print("Success: Error Traceback Preserved (Keep Error).")

        # Turn 6 검증 (QA 검증 수행)
        print("\n[TURN 6] Running QA Verification (QA_VERIFYING State)...")
        res6 = agent.run_step(turns[5])
        exec_res6 = res6["execution_result"]
        assert isinstance(exec_res6, dict)
        assert exec_res6.get("status") == "success"
        assert "QA Verification PASSED" in exec_res6.get("output", "")
        print(f"Output: {exec_res6['output']}")

        print("\n==================================================")
        print("  Demo Run Completed Successfully on all OS!")
        print("==================================================")
        
    except AssertionError as e:
        print(f"\n❌ DEMO RUN ASSERTION FAILED: {str(e)}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ DEMO RUN UNEXPECTED EXCEPTION: {str(e)}")
        sys.exit(1)
    finally:
        # SQLite 파일 잠금 해제를 위해 에이전트 루프 클로즈 명시적 호출
        agent.close()

if __name__ == "__main__":
    run_demo()
