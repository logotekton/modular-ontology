# UI/UX 디자인 하네스 (Design Harness) 구현 계획

디자인 하네스는 에이전트가 "예쁜 UI"를 만들도록 구두로 지시하는 대신, **코드와 시스템 규칙(Linter, Component, CSS Tokens)을 통해 고품질의 시각적 인터랙션을 강제**하는 아키텍처입니다.

## 1. 파일 및 컴포넌트 배치 계획

```
manus_harness/
│
├── design/
│   ├── __init__.py
│   ├── linter.py           # 생성된 HTML/CSS/React 코드를 검증하는 Python 린터 가드
│   └── tokens.css          # 물리 기반 이징 및 촉각 인터랙션 전역 CSS 토큰
│
└── react_components/       # React/Tailwind 프로젝트에 즉시 이식할 하네스 컴포넌트
    ├── TactileButton.tsx   # 물리 스케일 다운 및 snappy transition 강제 버튼
    ├── ContrastContainer.tsx # 배경 이미지 밝기 감지 및 텍스트 대비 자동 정합 컨테이너
    └── AspectCanvas.tsx    # 슬라이드(PPT) 16:9 캔버스 락 및 오버플로우 방지 래퍼
```

## 2. 5대 핵심 가이드라인의 코드 수준 강제화

1.  **물리 기반 이징(Cubic-Bezier)**: CSS 기본 `ease`를 차단하고, 촉각적 반응을 극대화하는 커스텀 이징 토큰을 CSS에 주입합니다.
2.  **대비 자동 정합**: 배경 이미지 스타일(High-Key / Low-Key)에 맞춰 텍스트 컬러를 자동 페어링합니다.
3.  **16:9 비율 캔버스 락**: 슬라이드 레이아웃 붕괴를 막기 위해 세로 스크롤을 제한하고 뷰포트 크기를 락킹합니다.
4.  **안티패턴 정적 검사**: React 무한 루프 코드, <a> 중첩, 컬러 페어링 누락을 린터로 사전 차단합니다.
