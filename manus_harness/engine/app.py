from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File, Form
from pydantic import BaseModel
from typing import Dict, Any, Optional
import os
import shutil
import threading
from contextlib import asynccontextmanager

# [P1 피드백 반영]: FastAPI 구동 시 기본 도구들이 레지스트리에 반드시 등록되도록 명시적 임포트 처리
import manus_harness.tools.base 
from manus_harness.engine.loop import HarnessAgentLoop
from manus_harness.config import settings

# 활성화된 에이전트 세션 저장소 (메모리 내 보관 - 실서비스에서는 Redis/DB 권장)
active_sessions: Dict[str, HarnessAgentLoop] = {}
active_sessions_lock = threading.RLock()

def _get_or_create_agent(session_id: str) -> tuple[HarnessAgentLoop, bool]:
    try:
        settings.get_session_dir(session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    with active_sessions_lock:
        agent = active_sessions.get(session_id)
        if agent is not None:
            return agent, False
        agent = HarnessAgentLoop(session_id=session_id)
        active_sessions[session_id] = agent
        return agent, True

def _close_all_sessions() -> None:
    with active_sessions_lock:
        agents = list(active_sessions.values())
        active_sessions.clear()
    for agent in agents:
        try:
            agent.close()
        except Exception:
            pass

# =====================================================================
# [P2 피드백 반영] FastAPI lifespan을 활용한 서버 셧다운 시 활성 세션 SQLite 커넥션 일괄 close() 처리
# =====================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: 아무 작업도 하지 않음
    yield
    # Shutdown: 모든 활성화된 세션의 SQLite 커넥션 및 루프 자원 일괄 해제
    _close_all_sessions()

app = FastAPI(
    title="Manus Harness Core API Server",
    description="실제 서비스 인프라에 통합 가능한 에이전트 하네스 API 서버입니다. (v1.3.4)",
    version="1.3.8",
    lifespan=lifespan
)

class StartSessionRequest(BaseModel):
    session_id: str

class AgentStepRequest(BaseModel):
    session_id: str
    user_prompt: Optional[str] = None
    mock_llm_decision: Optional[Dict[str, Any]] = None

@app.post("/v1/agent/start", summary="새로운 에이전트 하네스 세션 시작")
def start_session(request: StartSessionRequest):
    session_id = request.session_id
    agent, created = _get_or_create_agent(session_id)
    if not created:
        return {"status": "existing", "session_id": session_id, "message": "Session already active."}
    
    # 에이전트 루프 초기화 및 세션 등록 (todo.md가 이미 디스크에 있다면 자동 복구 복원)
    return {"status": "started", "session_id": session_id, "current_state": agent.state_machine.current_state}

@app.post("/v1/agent/step", summary="에이전트 하네스 루프 1단계 실행")
def execute_step(request: AgentStepRequest):
    session_id = request.session_id
    
    # [P1 피드백 반영]: 세션이 없으면 HarnessAgentLoop를 생성할 때 디스크의 todo.md/history.db로부터 상태가 완전 복구됩니다.
    agent, _ = _get_or_create_agent(session_id)

    # 1. LLM 결정 소스 선택 (전달된 Mock 결정 우선, 없을 경우 실제 LLM 호출 모듈 사용 가능)
    decision = request.mock_llm_decision
    if not decision:
        # 실제 LLM 호출을 위해 컨텍스트 메시지 구성
        current_state = agent.state_machine.current_state
        from manus_harness.tools.registry import tool_registry
        active_tools = tool_registry.get_active_tools_definition(agent.state_machine.current_state_enum)
        
        system_msg = agent.context_manager.get_system_message(active_tools)
        todo_content = agent.state_machine.read_todo()
        dynamic_msg = agent.context_manager.get_dynamic_context_message(current_state, todo_content)
        
        # [P2 수정] 메시지 순서 수정 — dynamic_msg(system)를 user 메시지 뒤가 아닌 첫 번째 system_msg 뒤에 배치하여 OpenAI API 호환성 준수
        dynamic_msg["role"] = "user"
        messages = [
            system_msg,
            dynamic_msg,
            {"role": "user", "content": request.user_prompt or "Continue the task plan."}
        ]
        
        from manus_harness.context.llm import LLMClient
        llm_client = LLMClient()
        decision = llm_client.get_next_decision(messages)

    # 2. 하네스 엔진 루프 실행
    step_result = agent.run_step(decision)
    
    return {
        "session_id": session_id,
        "current_state": step_result["current_state"],
        "execution_result": step_result["execution_result"],
        "decision_made": decision
    }

@app.get("/v1/agent/status/{session_id}", summary="세션의 현재 상태 및 계획(todo.md) 조회")
def get_status(session_id: str):
    # [P1 피드백 반영]: 디스크에서 todo.md를 파싱하여 자동 상태 복구 시도 (장애 복구 아키텍처)
    agent, _ = _get_or_create_agent(session_id)
        
    todo_content = agent.state_machine.read_todo()
    
    return {
        "session_id": session_id,
        "current_state": agent.state_machine.current_state,
        "todo_content": todo_content,
        "history_count": len(agent.context_manager.history)
    }

# =====================================================================
# [P2 피드백 반영] FastAPI 이미지 업로드 및 정밀 디자인 분석 엔드포인트 신규 구현
# =====================================================================
@app.post("/v1/design/analyze", summary="업로드된 디자인 시안 이미지 정밀 분석 (OpenAI Vision)")
async def analyze_design(
    file: UploadFile = File(..., description="업로드할 디자인 시안 이미지 파일"),
    prompt: Optional[str] = Form(None, description="분석 유도용 추가 지시 프롬프트")
):
    """
    사용자가 업로드한 디자인 시안 이미지(또는 스케치)를 수신하여 로컬 임시 디렉토리에 저장하고,
    OpenAI Vision API 및 Structured Outputs를 통해 정밀 분석한 DesignSpec 구조화 JSON 규격을 반환합니다.
    """
    # 1. 파일 확장자 검증 및 크기 제한 (P2 피드백 반영: 최대 10MB 크기 제한 및 확장자 정밀 매핑)
    allowed_exts = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif"
    }
    if not file.filename:
        raise HTTPException(status_code=400, detail="Uploaded file must include a filename.")

    safe_filename = os.path.basename(file.filename.replace("\\", "/"))
    ext = os.path.splitext(safe_filename.lower())[1]
    if ext not in allowed_exts:
        raise HTTPException(status_code=400, detail=f"Unsupported image extension '{ext}'. Allowed: {list(allowed_exts.keys())}")

    # [P2 피드백 반영] 스트림 읽기를 통한 대용량 파일 업로드 메모리 폭발 차단 및 크기 제한 검증
    MAX_SIZE = 10 * 1024 * 1024  # 10MB
    chunks = bytearray()
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > MAX_SIZE:
            raise HTTPException(status_code=400, detail=f"File size exceeds maximum limit of 10MB (Size: {len(chunks)} bytes)")
    contents = bytes(chunks)

    # [P2 피드백 반영] Pillow 라이브러리를 사용해 업로드된 바이트가 실제 유효한 이미지인지 정밀 검증
    from PIL import Image
    import io
    try:
        img = Image.open(io.BytesIO(contents))
        # Pillow가 인식한 실제 이미지 포맷 확인 및 검증
        detected_format = img.format.lower() if img.format else ""
        
        # verify() 이후에는 Pillow 이미지 객체를 다시 열어야 하므로 별도 핸들로 검증합니다.
        img_check = Image.open(io.BytesIO(contents))
        img_check.verify()  # 이미지 손상 여부 1차 검증
        mime_mapping = {
            "png": "image/png",
            "jpeg": "image/jpeg",
            "jpg": "image/jpeg",
            "webp": "image/webp",
            "gif": "image/gif"
        }
        
        # 확장자와 실제 감지된 포맷의 일관성 검증 (확장자 위장 방지)
        expected_format = "jpeg" if ext in [".jpg", ".jpeg"] else ext.lstrip(".")
        if detected_format != expected_format:
            # 특수한 경우(예: Pillow가 MPO 등을 jpeg로 읽는 경우)를 감안하되 기본 위장 방어
            if not (detected_format == "mpo" and expected_format == "jpeg"):
                raise HTTPException(status_code=400, detail=f"Image format mismatch. File header suggests '{detected_format}' but extension is '{ext}'")
                
        mime_type = mime_mapping.get(detected_format, allowed_exts[ext])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image file or corrupted content: {str(e)}")

    try:
        # 3. 로컬 파일 URL 변환 (실제 서비스에서는 S3/CDN 업로드 후 URL 전달, 여기서는 data-uri 스키마로 변환하여 Vision API에 안전 전달)
        import base64
        encoded_string = base64.b64encode(contents).decode('utf-8')
        data_uri = f"data:{mime_type};base64,{encoded_string}"

        # 4. OpenAI Vision API 및 Structured Outputs 분석 호출
        from manus_harness.context.llm import LLMClient
        llm_client = LLMClient()
        spec_result = llm_client.analyze_design_image(image_url=data_uri, prompt=prompt)

        return {
            "status": "success",
            "filename": safe_filename,
            "design_spec": spec_result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image design analysis failed: {str(e)}")
