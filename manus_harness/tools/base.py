import os
import shlex
import subprocess
from manus_harness.config import settings
from manus_harness.tools.registry import tool_registry
from manus_harness.engine.state import AgentState

# 1. 계획 및 분석 단계 전용 도구 (PLANNING 상태에서만 허용)
@tool_registry.register(
    name="brainstorm_and_plan",
    description="브레인스토밍 및 프로젝트 계획을 수립합니다. PLANNING 상태에서만 사용 가능합니다.",
    parameters={"topic": "string", "phases": "array"},
    allowed_states=[AgentState.PLANNING]
)
def brainstorm_and_plan(topic: str, phases: list) -> str:
    return f"브레인스토밍 완료: '{topic}'. {len(phases)}개의 마일스톤이 계획에 등록되었습니다."


# [P1 보안 강화 및 크로스플랫폼] 격리된 세션별 워크스페이스 내 안전한 명령어 실행기 (v1.3.4)
@tool_registry.register(
    name="execute_safe_command",
    description="구조화된 인자 배열(args)을 통해 셸 명령어를 안전하게 실행합니다. 작업 디렉토리가 세션 워크스페이스로 강제 고정되며, 외부 경로 및 위험 플래그가 원천 차단됩니다. EXECUTING 상태에서만 사용 가능합니다.",
    parameters={"session_id": "string", "executable": "string", "args": "array"},
    allowed_states=[AgentState.EXECUTING]
)
def execute_safe_command(session_id: str, executable: str, args: list) -> str:
    """
    구조화된 샌드박스형 명령어 실행기
    ------------------------------
    - session_id 격리 디렉토리의 절대 경로를 획득하여 작업 디렉토리(cwd)를 해당 경로로 강제 고정합니다.
    - command string 대신 {executable, args: [...]} 배열을 사용하여 Windows/Unix 모두에서 인용 부호 깨짐을 방지합니다.
    - [P1 보안] 위험한 플래그(예: python -c, 파일 시스템 탈출을 시도하는 인자 등)를 완벽히 검출하고 차단합니다.
    - [P2 보안 피드백 반영] python -m을 통한 임의의 모듈 실행 공격 표면을 원천 봉쇄하기 위해 기본적으로 차단하되,
      오직 사전에 허용된 안전한 모듈 화이트리스트(예: pytest, json.tool)만 허용합니다.
    - [P1 보안] 인자 내의 모든 경로 인자가 세션 워크스페이스를 이탈하지 못하도록 경로 경계를 철저히 통제합니다.
    """
    if not session_id:
        raise ValueError("session_id is required to enforce sandbox isolation.")
    if not executable:
        raise ValueError("Executable must be specified.")

    # 1. 격리된 세션 디렉토리 획득 및 경로 검증
    session_dir = settings.get_session_dir(session_id)
    resolved_session_dir = os.path.realpath(session_dir)

    # 2. SANDBOX_ENABLED 가드레일 작동
    if settings.SANDBOX_ENABLED:
        ALLOWED_EXECUTABLES = {
            "python", "python3", "pip", "pip3", "git", "ls", "mkdir", "cat"
        }
        
        exec_name = os.path.basename(executable).lower()
        if exec_name.endswith(".exe"):
            exec_name = exec_name[:-4]

        if (
            os.path.basename(executable) != executable
            or os.path.isabs(executable)
            or executable.startswith(("/", "\\"))
            or (len(executable) > 1 and executable[1] == ":")
        ):
            raise PermissionError(
                "Sandbox Security Violation: Executable must be a bare allowlisted command name, not a path."
            )

        if exec_name not in ALLOWED_EXECUTABLES:
            raise PermissionError(
                f"Sandbox Security Violation: Executable '{exec_name}' is not in the allowlist. "
                f"Allowed executables: {sorted(list(ALLOWED_EXECUTABLES))}"
            )

        # [P2 보안 피드백 반영] pip, git 등의 무분별한 네트워크 설치 및 외부 레포지토리 클론 차단용 서브커맨드 허용 목록 적용
        if exec_name in ("pip", "pip3"):
            # pip, pip3는 오직 패키지 정보 조회, 목록 조회, 도움말 확인 등 읽기 전용 서브커맨드만 허용합니다. (install, uninstall, download 등 차단)
            ALLOWED_PIP_SUBCOMMANDS = {"list", "show", "help", "--help", "-h"}
            FORBIDDEN_PIP_ARGS = {
                "--outdated", "-o",
                "--index-url", "-i",
                "--extra-index-url",
                "--find-links", "-f",
                "--trusted-host",
                "--proxy",
                "--cert",
                "--client-cert",
            }
            # 첫 번째 인자가 서브커맨드인지 확인
            subcmd = None
            for arg in args:
                if not arg.startswith("-"):
                    subcmd = arg.lower()
                    break
            if subcmd and subcmd not in ALLOWED_PIP_SUBCOMMANDS:
                raise PermissionError(
                    f"Sandbox Security Violation: Subcommand '{subcmd}' is not allowed for '{exec_name}'. "
                    f"Allowed subcommands: {sorted(list(ALLOWED_PIP_SUBCOMMANDS))}"
                )
            # 인자 목록 전체에서 install 시도가 있는지 이중 방어
            if any(x.lower() == "install" for x in args):
                raise PermissionError(f"Sandbox Security Violation: Package installation via '{exec_name} install' is strictly prohibited.")
            for arg in args:
                lowered = arg.lower()
                if lowered in FORBIDDEN_PIP_ARGS or lowered.startswith(("http://", "https://")):
                    raise PermissionError(
                        f"Sandbox Security Violation: Network-affecting pip argument '{arg}' is prohibited."
                    )

        elif exec_name == "git":
            # git은 상태 조회, 로그 확인, 차이점 비교 등 로컬 읽기 전용 서브커맨드만 허용합니다. (clone, push, pull, fetch, remote 등 외부 네트워크 연동 차단)
            ALLOWED_GIT_SUBCOMMANDS = {"status", "log", "diff", "show", "branch", "help", "--help", "-h"}
            subcmd = None
            for arg in args:
                if not arg.startswith("-"):
                    subcmd = arg.lower()
                    break
            if subcmd and subcmd not in ALLOWED_GIT_SUBCOMMANDS:
                raise PermissionError(
                    f"Sandbox Security Violation: Subcommand '{subcmd}' is not allowed for git. "
                    f"Allowed subcommands: {sorted(list(ALLOWED_GIT_SUBCOMMANDS))}"
                )
            # 인자 목록 전체에서 clone/pull 등 차단 이중 방어
            FORBIDDEN_GIT_ARGS = {"clone", "pull", "push", "fetch", "remote"}
            if any(x.lower() in FORBIDDEN_GIT_ARGS for x in args):
                raise PermissionError("Sandbox Security Violation: Remote git operations (clone, pull, push, fetch) are strictly prohibited.")

        # [P1 보안] 위험 플래그 및 원격 코드 실행(RCE) 가드레일
        # [P2 보안 피드백 반영] python -m pytest 등 안전한 유틸리티 모듈만 허용하고, 임의 모듈 실행을 통한 우회 RCE를 철저히 차단합니다.
        ALLOWED_PYTHON_MODULES = {"pytest", "json.tool"}
        
        for i, arg in enumerate(args):
            arg_lower = arg.lower()
            if arg_lower in ("-c", "--code"):
                raise PermissionError(
                    f"Sandbox Security Violation: Inline code execution flag '{arg}' is strictly prohibited."
                )
            
            if exec_name in ("python", "python3") and arg_lower == "-m":
                # -m 뒤에 오는 모듈 이름 검증
                if i + 1 < len(args):
                    target_module = args[i + 1].lower()
                    if target_module not in ALLOWED_PYTHON_MODULES:
                        raise PermissionError(
                            f"Sandbox Security Violation: Executing python module '{target_module}' via -m is strictly prohibited. "
                            f"Allowed modules: {sorted(list(ALLOWED_PYTHON_MODULES))}"
                        )
                else:
                    raise PermissionError("Sandbox Security Violation: Missing module name after -m flag.")

            # [P1 보안] 인자 내 경로 탈출(Path Traversal) 검증
            # 인자에 포함된 경로가 세션 워크스페이스 외부를 가리키거나 탈출 시도가 감지되면 차단합니다.
            if (
                ".." in arg
                or os.path.isabs(arg)
                or arg.startswith(("/", "\\"))
                or (len(arg) > 1 and arg[1] == ":")
            ):
                # 절대 경로 정규화 후 containment check 수행
                try:
                    resolved_arg_path = os.path.realpath(os.path.join(resolved_session_dir, arg))
                    sep = os.path.sep
                    prefix = resolved_session_dir if resolved_session_dir.endswith(sep) else resolved_session_dir + sep
                    if not resolved_arg_path.startswith(prefix) and resolved_arg_path != resolved_session_dir:
                        raise PermissionError(
                            f"Sandbox Security Violation: Argument path '{arg}' escapes the session workspace."
                        )
                except PermissionError:
                    raise
                except Exception:
                    # 정규화 불가능한 문자열의 경우 안전을 위해 예외 차단 처리
                    raise PermissionError(
                        f"Sandbox Security Violation: Unverifiable path argument '{arg}' detected."
                    )

    full_args = [executable] + args

    # 3. Windows python3 명령어 포워딩
    if settings.IS_WINDOWS and full_args[0] == "python3":
        full_args[0] = "python"

    # 4. subprocess 실행 (shell=False 강제, cwd를 세션 격리 경로로 강제 고정)
    # [P2 피드백 반영] Windows cp949 환경에서도 stderr/stdout 인코딩이 깨지지 않고 traceback을 정상 보존하도록
    # encoding="utf-8" 및 errors="replace" 사양을 명시적으로 설정합니다.
    try:
        result = subprocess.run(
            full_args,
            shell=False,
            cwd=resolved_session_dir, # 세션 격리 경로로 고정
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15
        )
        
        if result.returncode != 0:
            raise RuntimeError(
                f"Command failed with exit code {result.returncode}.\n"
                f"Stdout: {result.stdout}\n"
                f"Stderr: {result.stderr}"
            )
        return result.stdout
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(f"Command execution timed out after 15 seconds. Output so far:\n{e.stdout}")
    except FileNotFoundError as e:
        raise FileNotFoundError(f"Executable file not found: {full_args[0]}. Detail: {str(e)}")


# 2. 기존 실행 단계 전용 도구 (사용이 권장되지 않는 [DEPRECATED] 도구로 지정)
# [P1 피드백 반영] allowed_states=[]를 통해 어떠한 상태에서도 이 도구가 노출되거나 실행되지 않도록 마스킹합니다.
@tool_registry.register(
    name="execute_shell_command",
    description="[DEPRECATED] 격리된 샌드박스 환경 내에서 셸 명령어를 실행합니다. 보안 및 크로스플랫폼 정합성을 위해 신규 세션 기반 도구인 'execute_safe_command'를 사용하십시오.",
    parameters={"command": "string"},
    allowed_states=[]
)
def execute_shell_command(command: str) -> str:
    """
    [DEPRECATED] 레거시 셸 명령어 실행기
    """
    raise PermissionError("execute_shell_command is deprecated and disabled. Use execute_safe_command instead.")



# [P1/P2 보안 수정] 디자인 린터 에이전트 루프 연동 및 세션 워크스페이스 격리 도구 (v1.3.4)
@tool_registry.register(
    name="lint_and_write_frontend_code",
    description="생성된 프론트엔드 코드를 디자인 린터로 정적 분석하고 안전하게 자가 교정한 후 세션 워크스페이스 내부에 파일로 저장합니다. EXECUTING 상태에서만 사용 가능합니다.",
    parameters={"session_id": "string", "code": "string", "file_path": "string", "design_contract": "object"},
    allowed_states=[AgentState.EXECUTING]
)
def lint_and_write_frontend_code(session_id: str, code: str, file_path: str, design_contract=None) -> str:
    """
    디자인 린터 정적 검증 및 자동 자가 교정 아키텍처
    ---------------------------------------------
    - [P1 보안] session_id 기반 격리 경로를 획득하여, 파일 쓰기 타겟 경로가 세션 워크스페이스 외부로 탈출하는 행위를 원천 차단합니다.
    - [P1 피드백 반영] 이 도구는 오직 프론트엔드 코드 전용이므로, 실행 가능한 Python 파일(.py)이나 셸 스크립트 쓰기를 철저히 차단하여 우회 RCE를 원천 봉쇄합니다.
    - 에이전트가 코드를 디스크에 쓰기 전 디자인 린터(`DesignLinter`)를 강제 통과시킵니다.
    - 안티패턴(nested_anchors, fixed_height 등) 발견 시 자가 교정(`enforce_safe_design`)을 우선 시도합니다.
    - 교정 후에도 해결 불가능한 CRITICAL 결함(unstable_render_reference 등)이 잔존하면 파일 쓰기를 전면 중단하고 에러 처리(오류 보존 법칙)합니다.
    """
    if not session_id:
        raise ValueError("session_id is required to enforce workspace containment.")

    # [P1 피드백 반영] 실행 가능한 위험 스크립트 확장자 쓰기 시도 차단
    ext = os.path.splitext(file_path.lower())[1]
    FORBIDDEN_EXTENSIONS = {".py", ".pyw", ".sh", ".bash", ".bat", ".cmd", ".ps1", ".exe", ".pl", ".rb"}
    if ext in FORBIDDEN_EXTENSIONS:
        raise PermissionError(
            f"Sandbox Security Violation: Writing executable script files ('{ext}') is strictly prohibited via this tool. "
            f"Only frontend files (.tsx, .jsx, .js, .html, .css, .json 등) are allowed."
        )

    from manus_harness.design.linter import design_linter

    def scan_code(candidate_code):
        generic_scan = design_linter.lint_code(candidate_code)
        findings = list(generic_scan["findings"])
        is_safe = generic_scan["is_safe"]
        if design_contract:
            contract_scan = design_linter.lint_reconstruction_contract(candidate_code, design_contract)
            findings.extend(contract_scan["findings"])
            is_safe = is_safe and contract_scan["is_safe"]
        return {
            "is_safe": is_safe,
            "findings": findings,
            "findings_count": len(findings)
        }
    
    # 1. 1차 정적 린팅 수행
    first_scan = scan_code(code)
    
    final_code = code
    if not first_scan["is_safe"] or first_scan["findings_count"] > 0:
        print(f"[DESIGN LINTER] {first_scan['findings_count']} design violations detected. Attempting automatic safe design enforcement...")
        
        # 2. 자동 자가 교정 수행
        final_code = design_linter.enforce_safe_design(code)
        
        # 3. 교정 후 2차 재검사
        second_scan = scan_code(final_code)
        
        # 교정 후에도 해결 불가능한 CRITICAL 결함이 남아 있으면 실패 처리 (오류 보존 법칙 유도)
        if not second_scan["is_safe"]:
            critical_findings = [f for f in second_scan["findings"] if f["severity"] == "CRITICAL"]
            raise RuntimeError(
                f"Design Harness Violation: Code contains uncorrectable CRITICAL design anti-patterns. File write blocked.\n"
                f"Unresolved Critical Findings:\n" + "\n".join([f"Line {f['line']}: [{f['rule']}] {f['message']} (Snippet: {f['matched_text']})" for f in critical_findings])
            )
        
        print("[DESIGN LINTER] Safe design enforced successfully. All CRITICAL issues resolved.")
    
    # 4. [P1 보안] 세션 워크스페이스 경로 획득 및 이탈(Path Traversal) 정밀 검증
    session_dir = settings.get_session_dir(session_id)
    resolved_session_dir = os.path.realpath(session_dir)
    
    # 입력된 파일 경로 정규화 및 결합
    target_path = os.path.join(resolved_session_dir, file_path)
    resolved_target_path = os.path.realpath(target_path)
    
    # 세션 워크스페이스 내부인지 이중 검증
    sep = os.path.sep
    prefix = resolved_session_dir if resolved_session_dir.endswith(sep) else resolved_session_dir + sep
    if not resolved_target_path.startswith(prefix) and resolved_target_path != resolved_session_dir:
        raise PermissionError(
            f"Sandbox Security Violation: Write target path escapes the session workspace. "
            f"Target: '{file_path}'"
        )
    
    # 디렉토리 생성 및 파일 쓰기
    dir_name = os.path.dirname(resolved_target_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
        
    with open(resolved_target_path, "w", encoding="utf-8") as f:
        with_newline = final_code if final_code.endswith("\n") else final_code + "\n"
        f.write(with_newline)
        
    return f"Success: Code linted, safe design enforced, and written successfully to '{file_path}' inside session workspace."


# 3. 품질 검증 단계 전용 도구 (QA_VERIFYING 상태에서만 허용)
# [P1 피드백 반영] run_qa_verification도 session_id 경로 격리 검증을 추가하여 파일 존재 여부 노출 취약점을 완벽히 해소합니다.
@tool_registry.register(
    name="run_qa_verification",
    description="산출물의 무결성 및 코드 린트, 빌드 적합성을 테스트합니다. QA_VERIFYING 상태에서만 사용 가능합니다.",
    parameters={"session_id": "string", "target_path": "string"},
    allowed_states=[AgentState.QA_VERIFYING]
)
def run_qa_verification(session_id: str, target_path: str) -> str:
    if not session_id:
        raise ValueError("session_id is required to enforce QA containment.")

    session_dir = settings.get_session_dir(session_id)
    resolved_session_dir = os.path.realpath(session_dir)
    
    # 입력된 타깃 경로 결합 및 정규화
    full_target_path = os.path.join(resolved_session_dir, target_path)
    resolved_target_path = os.path.realpath(full_target_path)

    # 세션 워크스페이스 내부인지 이중 검증 (정보 노출 방지)
    sep = os.path.sep
    prefix = resolved_session_dir if resolved_session_dir.endswith(sep) else resolved_session_dir + sep
    if not resolved_target_path.startswith(prefix) and resolved_target_path != resolved_session_dir:
        raise PermissionError(
            f"Sandbox Security Violation: QA target path '{target_path}' escapes the session workspace."
        )

    if not os.path.exists(resolved_target_path):
        raise FileNotFoundError(f"QA Verification Failed: Target path '{target_path}' does not exist inside session workspace.")
        
    return f"QA Verification PASSED for target: '{target_path}'. All build and style guidelines are met."


# 4. LLM 오류 전용 로깅 도구 (모든 상태에서 허용)
@tool_registry.register(
    name="log_error",
    description="LLM 의사결정 중 발생한 예외 상황이나 API 에러를 기록합니다. 모든 상태에서 호출 가능합니다.",
    parameters={"error": "string"},
    allowed_states=[AgentState.INITIAL, AgentState.PLANNING, AgentState.EXECUTING, AgentState.QA_VERIFYING, AgentState.FAILED]
)
def log_error(error: str) -> str:
    print(f"[HARNESS LOG ERROR] {error}")
    return f"Error logged successfully: {error}"


# 5. FAILED 상태 복구 및 PLANNING 전이 도구 (FAILED 상태에서만 허용)
@tool_registry.register(
    name="recover_failed_state",
    description="에이전트가 FAILED 상태에 빠졌을 때 원인을 확인하고 PLANNING 상태로 재전이하여 태스크를 복구합니다.",
    parameters={"reason": "string"},
    allowed_states=[AgentState.FAILED]
)
def recover_failed_state(reason: str) -> dict:
    """
    [P1 상태 머신 피드백 반영] 루프 엔진과의 계약에 맞춰 정확한 전이 액션(action: transition) 구조를 반환합니다.
    """
    return {
        "action": "transition",
        "target": "PLANNING",
        "status": "success",
        "output": f"Recovery initiated. Reason for failure: '{reason}'. Re-transitioning to PLANNING state."
    }
