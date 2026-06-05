import React from "react";

interface AspectCanvasProps extends React.HTMLAttributes<HTMLDivElement> {
  children: React.ReactNode;
}

/**
 * AspectCanvas: 슬라이드(PPT) 렌더링 시 레이아웃 붕괴를 원천 차단하는 하네스 컴포넌트
 * 
 * [P2 수정] aspect-[16/9] 비율 락과 min-h-[720px]의 충돌을 방지하기 위해:
 * - 데스크톱 및 표준 환경에서는 aspect-[16/9] 비율을 강제 락하여 16:9 캔버스를 보장합니다.
 * - 좁은 폭의 뷰포트(예: 모바일, 태블릿)에서 16:9 비율이 강제되어 720px 미만으로 수축할 때, 
 *   콘텐츠가 잘리거나 깨지는 오버플로우 리스크를 막기 위해 미디어 쿼리를 사용하여 
 *   md(768px) 이상 뷰포트에서만 16:9 비율 락을 적용하고, 모바일에서는 유연하게 세로로 흐르도록 아키텍처를 최적화합니다.
 */
export const AspectCanvas: React.FC<AspectCanvasProps> = ({
  children,
  className = "",
  ...props
}) => {
  return (
    <div
      className={`w-full max-w-7xl mx-auto md:aspect-[16/9] min-h-[400px] md:min-h-[720px] bg-background text-foreground border border-border/50 rounded-xl shadow-2xl overflow-y-auto md:overflow-hidden flex flex-col justify-between p-6 md:p-12 harness-grid-bg relative ${className}`}
      {...props}
    >
      {/* 장식용 테크니컬 가이드라인 레이아웃 - 네오 테크니컬 디자인 메타포 */}
      <div className="absolute top-4 left-4 text-[10px] font-mono text-muted-foreground/40 select-none">
        MANUS HARNESS // CANVAS_LOCK_16_9
      </div>
      <div className="absolute bottom-4 right-4 text-[10px] font-mono text-muted-foreground/40 select-none">
        STATUS: SAFE_ISOMER_ACTIVE
      </div>

      <div className="w-full h-full flex flex-col justify-between">
        {children}
      </div>
    </div>
  );
};

export default AspectCanvas;
