# 🎨 Manus UI/UX 디자인 하네스 (Design Harness) v1.3.4 기술 사양서

본 문서는 에이전트가 프론트엔드 코드(React, Tailwind, HTML)를 생성할 때 발생할 수 있는 **AI Slop(양산형 디자인)** 및 **인터랙션/레이아웃 안티패턴**을 차단하고, 타협 없는 최상위 수준의 UI/UX를 일관되게 보장하기 위해 설계된 **디자인 하네스(Design Harness) v1.3.4**의 기술 명세서입니다.

---

## 1. 아키텍처 배경 및 설계 사상

자율 에이전트가 생성하는 프론트엔드 코드는 종종 다음과 같은 "양산형 AI 흔적(AI Slop)"을 남깁니다:
- **무의미한 전역 중앙 정렬** (`items-center justify-center` 남용)
- **부적절한 텍스트 가독성** (어두운 이미지 배경 위에 검은색 텍스트 배치)
- **반응형 레이아웃 붕괴** (16:9 캔버스 락과 고정 높이 `h-[600px]` 충돌로 인한 모바일 뷰포트 깨짐)
- **무한 루프 유발 상태 참조** (렌더링 페이즈 내 `new Date()` 등 참조 타입 무한 인스턴스화)

디자인 하네스 v1.3.4는 이러한 문제를 정적 분석(AST/토큰 정적 린터)과 런타임 제약 컴포넌트(React UI Primitives)의 결합을 통해 완벽하게 교정하며, **v1.3.4 통합 패치**를 통해 다음과 같은 핵심 엔터프라이즈 사양을 달성했습니다:
1. **에이전트 루프 저장 도구(`lint_and_write_frontend_code`) 연동**: 코드를 쓰기 전 린팅 및 자가 교정을 강제하고, 해결 불가능한 CRITICAL 결함 잔존 시 파일 저장을 차단하여 샌드박스 보안 격리를 유지합니다.
2. **OpenAI Vision 및 DesignSpec Structured Outputs 정식 탑재**: 이미지 시안을 분석하여 최적의 디자인 스펙을 구조화된 데이터로 추출합니다.
3. **SQLite3 기반 동시성 안전 로그 저장소**: 멀티세션 동시 실행 환경에서 완벽한 데이터 무결성을 보장합니다.

---

## 2. 디자인 하네스 컴포넌트 사양

### ① ContrastContainer (지능형 가독성 컨테이너)
배경 이미지의 명도(Luminance)를 분석하여 자식 텍스트의 색상 대비를 WCAG 2.1 AA 규격(4.5:1 이상)으로 강제 정합하는 지능형 컨테이너입니다.
- **Props**:
  - `imageUrl` / `bgImage`: 배경 이미지 URL (v1.3.4에서 두 속성 모두 유연하게 지원)
  - `fallbackColor`: 이미지 로드 실패 또는 부재 시 배경을 안전하게 채울 대체 색상 (v1.3.4에서 정식 구현 완료)
  - `className`: 사용자 정의 Tailwind CSS 클래스
- **v1.3.4 보정**: 텍스트 대비 색상 클래스(`text-slate-100` / `text-slate-900`)가 실제 자식 컴포넌트(`children`)들에게 CSS 상속되도록 렌더링 레이어 구조를 완벽하게 수정했습니다. 또한 이미지 로드 실패 시 가독성 레이아웃 및 `fallbackColor`로 자동 폴백되는 예외 처리 가드레일을 적용했습니다.

### ② AspectCanvas (반응형 16:9 고정 캔버스)
슬라이드(PPT)나 대시보드 뷰포트가 16:9 비율을 완벽히 유지하도록 제약하며, 좁은 화면에서의 레이아웃 깨짐 및 콘텐츠 잘림(Overflow)을 원천 차단하는 캔버스 하네스입니다.
- **v1.3.4 보정**: `aspect-[16/9]`와 `min-h-[720px]`의 레이아웃 충돌을 방지하기 위해, 데스크톱(md 이상) 환경에서만 16:9 비율 락을 적용하고, 모바일 환경에서는 스크롤바 노출 및 세로 흐름을 허용하도록 반응형 아키텍처를 적용했습니다.

### ③ TactileButton (촉각 피드백 버튼)
사용자의 클릭 인터랙션에 즉각적이고 물리적인 반응을 주는 고성능 마이크로 인터랙션 컴포넌트입니다.
- **v1.3.4 보정**: 물리 수축 스케일 값을 일관되게 `0.96`으로 고정하고, 하드코딩된 파란색 포커스 링 대신 디자인 시스템 토큰(`focus:ring-primary`) 및 다크모드 offset 링을 지원하도록 보정했습니다.

---

## 3. 정적 디자인 린터 규칙 및 자가 교정 (`design/linter.py`)

디자인 린터는 에이전트가 작성한 프론트엔드 코드를 분석하여 다음과 같은 5가지 규칙 위반을 탐지하고 자동으로 안전하게 교정합니다.

| 규칙 코드 | 위험 등급 | 검출 대상 (안티패턴) | 해결 대안 및 v1.3.1 개선 사항 |
| :--- | :--- | :--- | :--- |
| `unstable_render_reference` | **CRITICAL** | 렌더링 페이즈 내 `new Date()` 등 참조 인스턴스화 | `useState` 또는 `useMemo` 기반 참조 안정화 유도 (자동 교정 불가, 파일 저장 차단) |
| `nested_anchors` | **CRITICAL** | `<Link>` 내부에 중첩된 `<a>` 태그 배치 | 내부 `<a>` 태그를 `<span>` 또는 일반 텍스트로 자동 치환 및 정밀 정규식 교정 |
| `excessive_centered_layout` | **WARNING** | `items-center justify-center` 남용 | 양산형 중앙 정렬을 제거하고 그리드 및 비대칭 레이아웃 권장 (v1.3.4 토큰 기반 분석 보정) |
| `overflow_risk_fixed_height`| **WARNING** | `h-[600px]` 또는 `style={{ height: 600 }}` 리터럴 | 고정 높이를 `min-h-*` 또는 `minHeight`로 자동 자가 교정 및 스캔 범위 확대 |
| `missing_color_pairing` | **WARNING** | `bg-card` 계열 사용 시 텍스트 전경색 페어링 누락 | `text-card-foreground` 등의 대응 전경색 자동 삽입 (v1.3.4 다크모드/반응형 접두사 스플릿 및 중복 탐지 완벽 제거) |

---

## 4. 에이전트 루프 및 보안 가드레일 통합

v1.3.4 디자인 하네스는 에이전트가 생성한 코드를 디스크에 저장하기 전 강제로 린팅 및 자가 교정을 수행하는 핵심 도구로 이식되었습니다.

```python
# 에이전트 루프 연동 도구의 파일 시스템 격리 및 이중 방어 사양 (v1.3.4)
def lint_and_write_frontend_code(session_id: str, code: str, file_path: str) -> str:
    # 1. 세션 식별자를 통한 격리 워크스페이스 획득
    session_dir = settings.get_session_dir(session_id)
    resolved_path = os.path.realpath(os.path.join(session_dir, file_path))
    
    # 2. 이중 경로 탈출 차단 (Containment Check)
    if not resolved_path.startswith(os.path.realpath(session_dir)):
        raise PermissionError("Security Violation: File path escapes the session workspace.")
        
    # 3. 디자인 린터 구동 및 자동 자가 교정
    corrected_code = design_linter.enforce_safe_design(code)
    lint_result = design_linter.lint_code(corrected_code)
    
    # 4. 해결 불가능한 CRITICAL 결함 잔존 시 파일 쓰기 차단 및 에러 전송
    if not lint_result["is_safe"]:
        criticals = [f for f in lint_result["findings"] if f["severity"] == "CRITICAL"]
        if criticals:
            raise RuntimeError(f"Unresolved Critical Findings: {criticals}")
            
    # 5. 안전한 코드만 격리된 세션 내에 영구 저장
    with open(resolved_path, "w", encoding="utf-8") as f:
        f.write(corrected_code)
        
    return "Success: Code linted, safe design enforced, and written successfully."
```

이를 통해 자율 에이전트가 임의의 샌드박스 코드를 파괴하거나 외부 시스템으로 탈출하려는 모든 경로 우회 시도를 원천 봉쇄하면서, 고성능 프론트엔드 코드의 안전성을 보장합니다.
