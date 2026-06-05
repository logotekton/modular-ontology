# Manus Harness Core Engine (v1.3.4) & Design Harness (v1.3.4)

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Build Status: Passed](https://img.shields.io/badge/Build-Passed-green.svg)](#)

본 라이브러리는 Manus 자율 에이전트 시스템의 핵심 제어 루프를 리버스 엔지니어링하여 구현한 **크로스 플랫폼(Windows, Linux, macOS 지원) 에이전트 하네스 코어 엔진(v1.3.4)** 및 **UI/UX 디자인 하네스(v1.3.4)** 통합 패키지입니다.

에이전트의 상태 기반 도구 마스킹(Tool Masking), 예외 보존 법칙(Keep Error), 그리고 접두사 고정 컨텍스트 엔지니어링(Prefix-Locked Context Engineering) 기술을 실제 상용 프로덕션 수준의 API 서버와 디자인 품질 보증(QA) 인프라로 제공합니다.

---

## 1. 핵심 아키텍처 및 설계 원칙

하네스 v1.3.4는 자율 에이전트의 실행 제어 및 품질 관리를 위한 세 가지 핵심 아키텍처를 따릅니다:

```
  ┌────────────────────────────────────────────────────────────────────────┐
  │                 Prefix-Locked Context (KV-Cache Lock)                  │
  │  - System Prompt & Masked Tool Definitions (Locked at Top of Context)  │
  └───────────────────────────────────┬────────────────────────────────────┘
                                      ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │              Dynamic Context (Variable Input, Appended at Bottom)      │
  │  - Timestamp, Current State, Todo, Concurrency-Safe SQLite3 History     │
  └────────────────────────────────────────────────────────────────────────┘
```

1. **상태 기반 도구 마스킹 (Tool Masking)**: 에이전트의 현재 상태(`PLANNING`, `EXECUTING`, `QA_VERIFYING` 등)에 따라 허용된 도구 세트만 컨텍스트에 노출시켜 비정상적인 행동을 사전에 제약합니다.
2. **오류 보존 법칙 (Keep Error)**: 런타임 오류나 예외가 발생하더라도 에이전트 루프가 붕괴(Crash)되지 않고, 상세한 트레이스백(Traceback)을 동적 컨텍스트에 누적 보존하여 LLM의 자가 교정(Self-Correction)을 강력히 유도합니다.
3. **KV-Cache 최적화 컨텍스트 엔지니어링**: 시스템 프롬프트와 활성화된 도구 명세(고정 영역)를 컨텍스트 최상단에 고정 배치하여 LLM의 KV-Cache Hit Rate를 극대화하고, 시시각각 변하는 실행 이력(동적 영역)을 최하단에 정렬하여 상단 캐시 무효화를 원천 방지합니다.

---

## 2. 핵심 기능 및 업그레이드 사양 (v1.3.4)

- **[P1 보안] `execute_shell_command` 마스킹 버그 해결**: allowed_states가 빈 리스트(`[]`)일 때 도구가 활성화되던 오동작을 완벽히 교정하여, deprecated 도구가 어떠한 상태에서도 에이전트에게 노출되거나 실행되지 않도록 격리했습니다.
- **[P1 보안] Python 스크립트 RCE 우회 원천 차단**: 프론트엔드 코드 저장 도구(`lint_and_write_frontend_code`)에서 실행 가능한 스크립트 확장자(`.py`, `.sh`, `.bat` 등)의 파일 쓰기 시도를 차단하고 오직 안전한 프론트엔드 확장자만 허용하여 샌드박스 보안을 한층 더 강화했습니다.
- **[P2 동시성] SQLite3 WAL 모드 및 커넥션 영구 유지 최적화**: 멀티세션 동시 요청 시 락 경합을 최소화하기 위해 SQLite의 WAL(Write-Ahead Logging) 모드를 기본 활성화하고, 잦은 커넥션 오픈/클로즈로 인한 오버헤드와 자원 누수를 방지하기 위해 커넥션 유지 풀링 구조를 최적화했습니다.
- **[P2 기능] OpenAI Beta parse API 및 FastAPI 이미지 분석 엔드포인트**: OpenAI SDK의 공식 구조화 출력 API인 `client.beta.chat.completions.parse`를 사용하여 타입 안전성을 확보하고, FastAPI 서버에 이미지 정밀 분석을 위한 정식 엔드포인트(`POST /v1/design/analyze`)를 추가했습니다.
- **[P2 호환성] Windows stderr 디코딩 깨짐 해결**: `subprocess.run` 실행 시 stderr/stdout 리더에 인코딩을 `utf-8` 및 `errors="replace"`로 강제하여, Windows cp949 콘솔 환경에서도 트레이스백 보존 및 분석이 깨지지 않고 정상 기록되도록 보정했습니다.
- **[P3 디자인] `ContrastContainer` fallbackColor 완벽 정합**: 디자인 기술 사양서와의 정합성을 위해 `ContrastContainer.tsx` 컴포넌트에 `fallbackColor` prop을 추가하여 이미지 부재 시 안전하게 대체 색상 배경을 제공하도록 정합했습니다.

---

## 3. 디렉토리 구조

```
manus_harness/
├── __init__.py          # 패키지 버전 선언 (v1.3.4)
├── config.py            # 전역 환경 설정 및 세션 디렉토리 보안 검증 (v1.3.4)
├── context/
│   ├── __init__.py
│   ├── llm.py           # OpenAI Beta parse API 및 DesignSpec Structured Outputs 연동 (v1.3.4)
│   ├── manager.py       # SQLite3 WAL 모드 기반 동시성 안전 세션 로그 매니저 (v1.3.4)
│   └── templates.py     # Prefix-Locked 고정 시스템 프롬프트 템플릿
├── design/
│   ├── __init__.py
│   └── linter.py        # AST/토큰 기반 지능형 정적 디자인 린터 (v1.3.4)
├── engine/
│   ├── __init__.py
│   ├── app.py           # FastAPI 프로덕션 통합 API 서버 및 이미지 분석 엔드포인트 (v1.3.4)
│   ├── loop.py          # Manus형 에이전트 제어 루프 코어 (v1.3.4)
│   └── state.py         # 디스크(todo.md) 자동 상태 복구 유한 상태 머신
├── react_components/    # 디자인 하네스 React UI Primitives (v1.3.4)
│   ├── AspectCanvas.tsx
│   ├── ContrastContainer.tsx
│   └── TactileButton.tsx
├── tests/               # 12개 코어 및 디자인 단위/통합 테스트 스위트
│   ├── test_design_harness.py
│   └── test_harness.py
└── examples/
    └── run_harness.py   # 격리 및 보안 가드레일을 완벽 준수하는 데모 시나리오 (v1.3.4)
```

---

## 4. API 서버 구동 및 통합 가이드

### API 서버 실행
FastAPI 서버를 기동하여 에이전트 제어 인프라를 로컬 및 분산 클라우드 환경에 즉시 통합할 수 있습니다.

```bash
# FastAPI 구동 (기본 포트 8000)
uvicorn manus_harness.engine.app:app --host 0.0.0.0 --port 8000 --reload
```

### 핵심 엔드포인트 사양
- **`POST /v1/agent/start`**: 새로운 에이전트 세션을 시작합니다. 디스크에 기존 `todo.md`가 존재할 경우 자동으로 상태를 복구하여 복원합니다.
- **`POST /v1/agent/step`**: 에이전트 루프의 1단계를 실행합니다. 도구 실행 결과와 업데이트된 컨텍스트를 반환합니다.
- **`GET /v1/agent/status/{session_id}`**: 세션의 현재 상태와 계획서(`todo.md`) 내용을 안전하게 조회합니다.
- **`POST /v1/design/analyze`**: 사용자가 업로드한 시안 이미지를 수신하여 OpenAI Vision 및 구조화된 `DesignSpec`으로 정밀 분석한 규격을 반환합니다.

---

## 5. 라이선스 및 문의

본 프로젝트는 MIT 라이선스에 따라 자유롭게 배포 및 수정할 수 있습니다. 추가적인 문의사항이나 기술 지원 요청은 공식 메인프레임워크 관리자에게 문의해 주십시오.
