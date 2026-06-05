import json
from typing import Dict, Any, List, Optional
from openai import OpenAI
from pydantic import BaseModel, Field
from manus_harness.config import settings

# =====================================================================
# [P2 피드백 반영] OpenAI Beta chat.completions.parse 전용 Pydantic 스키마 정의
# =====================================================================

class ToolCallSchema(BaseModel):
    name: str = Field(description="실행할 도구의 이름")
    arguments: Dict[str, Any] = Field(description="도구 실행에 필요한 파라미터들")

class AgentDecision(BaseModel):
    transition_state: str = Field(
        description="에이전트의 다음 전이 상태",
        json_schema_extra={"enum": ["PLANNING", "EXECUTING", "QA_VERIFYING", "COMPLETED", "FAILED"]}
    )
    tool_call: Optional[ToolCallSchema] = Field(default=None, description="상태 전이 시 동반 호출할 도구 명세")

class ComponentSpec(BaseModel):
    name: str = Field(description="컴포넌트 이름")
    purpose: str = Field(description="컴포넌트 구현 목적 및 역할")
    critical_requirements: str = Field(description="구현 시 반드시 만족해야 하는 핵심 요구사항")

class ColorPaletteSpec(BaseModel):
    primary: str = Field(description="Primary Hex Color (e.g. #0F172A)")
    secondary: str = Field(description="Secondary Hex Color (e.g. #3B82F6)")
    background: str = Field(description="Background Hex Color (e.g. #F8FAFC)")
    foreground: str = Field(description="Foreground Hex Color (e.g. #1E293B)")

class TypographySpec(BaseModel):
    display_font: str = Field(description="Display Title Font (e.g. Space Grotesk)")
    body_font: str = Field(description="Body/Paragraph Font (e.g. Inter)")

class VisualFidelityContract(BaseModel):
    theme_mode: str = Field(default="unknown", description="Source image theme mode: light, dark, mixed, or unknown")
    required_regions: List[str] = Field(default_factory=list, description="Layout regions that must be preserved, e.g. topbar, sidebar, kpi_strip, process_flow, production_table")
    required_text: List[str] = Field(default_factory=list, description="Visible source-image labels that must appear in the generated UI")
    required_kpi_labels: List[str] = Field(default_factory=list, description="KPI labels that must be preserved")
    min_table_count: int = Field(default=0, description="Minimum table-like structures required to preserve source density")
    min_kpi_count: int = Field(default=0, description="Minimum KPI count required")
    min_data_rows: int = Field(default=0, description="Minimum data rows required across tables/lists")
    strict_density: bool = Field(default=False, description="When true, data density misses are blocking")

class DesignSpec(BaseModel):
    layout_paradigm: str = Field(description="분석된 최적의 레이아웃 구조 (예: asymmetric_grid, sidebar_dashboard, vertical_rhythm)")
    color_palette: ColorPaletteSpec = Field(description="추출된 테마 색상 팔레트")
    typography: TypographySpec = Field(description="폰트 페어링 가이드라인")
    components_to_implement: List[ComponentSpec] = Field(description="구현 대상 핵심 컴포넌트 목록")
    visual_fidelity_contract: VisualFidelityContract = Field(default_factory=VisualFidelityContract, description="Blocking reconstruction constraints extracted from the source image")


class LLMClient:
    """
    OpenAI LLM API 연동, Vision 이미지 분석 및 구조화된 출력(Structured Outputs) 제어기 (v1.3.4)
    ----------------------------------------------------------------------------------
    - LLM의 상태 전이 및 도구 호출 결정을 정확한 JSON 스키마로 파싱합니다.
    - [P2 기능 추가] OpenAI Vision API를 정식 연동하여 이미지 분석(DesignSpec 생성) 및 React 프론트엔드 코드 매핑을 지원합니다.
    """
    def __init__(self, api_key: str = settings.OPENAI_API_KEY):
        # 데모 구동 및 목업(Mock) 실행 유연성을 위해 Mock API 키 처리 지원
        self.is_mock = api_key == "mock-key-for-harness-demo"
        if not self.is_mock:
            self.client = OpenAI(api_key=api_key)
        else:
            self.client = None

    def get_next_decision(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        LLM API를 호출하여 에이전트의 다음 행동 결정을 반환합니다.
        [P2 피드백 반영] OpenAI Beta SDK의 공식 구조화 출력 API인 client.beta.chat.completions.parse를 사용하여
        JSON 스키마 누락 및 파싱 오류를 원천 차단하고 타입 안전성(Type Safety)을 극대화합니다.
        """
        if self.is_mock:
            # Mock 모드: 기본 기획 수립 결정을 시뮬레이션 반환
            return {
                "transition_state": "PLANNING",
                "tool_call": {
                    "name": "brainstorm_and_plan",
                    "arguments": {
                        "topic": "Modular BIM Parser",
                        "phases": ["Design", "Build", "QA"]
                    }
                }
            }

        try:
            # [P2 피드백 반영] client.beta.chat.completions.parse API 활용
            response = self.client.beta.chat.completions.parse(
                model=settings.OPENAI_MODEL,
                messages=messages,
                response_format=AgentDecision,
                temperature=0.1 # 결정론적 행동을 위해 낮은 온도로 설정
            )
            parsed_data = response.choices[0].message.parsed
            if not parsed_data:
                raise ValueError("Parsed decision is empty.")
            
            # Pydantic 모델을 dict로 변환
            return parsed_data.model_dump()
        except Exception as e:
            # LLM 호출 실패 시 FAILED 상태 전이 반환 및 log_error 도구 지정
            return {
                "transition_state": "FAILED",
                "tool_call": {
                    "name": "log_error",
                    "arguments": {"error": f"LLM API Call Failed: {str(e)}"}
                }
            }

    def analyze_design_image(self, image_url: str, prompt: Optional[str] = None) -> Dict[str, Any]:
        """
        [P2 기능 추가] OpenAI Vision API를 사용하여 업로드된 이미지(UI 시안, 설계 스케치 등)를 정밀 분석하고,
        구조화된 DesignSpec 규격을 Structured Outputs 형태로 안전하게 파싱 및 반환합니다.
        """
        if self.is_mock:
            # Mock 모드: 기본 철골 모듈러 디자인 스펙 반환
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
                "components_to_implement": [
                    {
                        "name": "ContrastContainer",
                        "purpose": "시각적 대비가 확보된 텍스트 및 이미지 레이어링",
                        "critical_requirements": "텍스트 대비 보장을 위해 children wrapper에 직접 text-slate-100 클래스를 결합할 것"
                    }
                ],
                "visual_fidelity_contract": {
                    "theme_mode": "light",
                    "required_regions": ["topbar", "sidebar", "kpi_strip"],
                    "required_text": ["OEE", "Throughput", "Work Orders"],
                    "required_kpi_labels": ["OEE", "Throughput", "Work Orders"],
                    "min_table_count": 1,
                    "min_kpi_count": 3,
                    "min_data_rows": 4,
                    "strict_density": False
                }
            }

        default_prompt = (
            "이 디자인 시안 또는 스케치 이미지를 정밀 분석하여 React 및 Tailwind CSS로 구현하기 위한 구조화된 디자인 스펙을 생성해 주십시오. "
            "특히 철골 모듈러 도메인의 기하학적이고 단단한 구조가 레이아웃 패러다임과 폰트 페어링에 반영되어야 합니다."
        )
        user_prompt = prompt if prompt else default_prompt

        try:
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_url
                            }
                        }
                    ]
                }
            ]

            # [P2 피드백 반영] client.beta.chat.completions.parse API 활용
            response = self.client.beta.chat.completions.parse(
                model=settings.OPENAI_MODEL,
                messages=messages,
                response_format=DesignSpec,
                temperature=0.2
            )
            parsed_data = response.choices[0].message.parsed
            if not parsed_data:
                raise ValueError("Parsed design spec is empty.")
                
            return parsed_data.model_dump()
        except Exception as e:
            raise RuntimeError(f"OpenAI Vision DesignSpec Generation Failed: {str(e)}")
