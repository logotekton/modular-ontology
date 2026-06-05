import React, { useEffect, useState } from "react";

interface ContrastContainerProps extends React.HTMLAttributes<HTMLDivElement> {
  imageUrl?: string;
  bgImage?: string; // README 예시 명세와의 정합성을 위한 별칭(Alias) 지원
  fallbackColor?: string; // [P3 수정] 이미지 로드 실패 시 적용할 배경 색상 명세 추가
  children: React.ReactNode;
}

/**
 * ContrastContainer: 배경 이미지의 명도(High-Key / Low-Key)를 자동 감지하여
 * 텍스트 대비 정합성(Contrast Rule)을 강제하는 하네스 컨테이너 컴포넌트
 */
export const ContrastContainer: React.FC<ContrastContainerProps> = ({
  imageUrl,
  bgImage,
  fallbackColor = "rgba(15, 23, 42, 0.9)", // 기본값은 어두운 slate-900 계열
  children,
  className = "",
  style = {},
  ...props
}) => {
  const activeImageUrl = imageUrl || bgImage || "";
  const [isLowKey, setIsLowKey] = useState<boolean>(true); // 기본값은 어두운 배경(Light Text)
  const [imageFailed, setImageFailed] = useState<boolean>(false);

  useEffect(() => {
    if (!activeImageUrl) {
      setImageFailed(true);
      return;
    }

    setImageFailed(false);
    const img = new Image();
    img.crossOrigin = "Anonymous";
    img.src = activeImageUrl;
    
    img.onload = () => {
      try {
        // 1. 캔버스를 이용한 이미지 픽셀 평균 명도 계산
        const canvas = document.createElement("canvas");
        const ctx = canvas.getContext("2d");
        if (!ctx) return;

        canvas.width = 10; // 성능을 위해 10x10 초소형으로 렌더링
        canvas.height = 10;
        ctx.drawImage(img, 0, 0, 10, 10);

        const imgData = ctx.getImageData(0, 0, 10, 10);
        let totalBrightness = 0;

        for (let i = 0; i < imgData.data.length; i += 4) {
          const r = imgData.data[i];
          const g = imgData.data[i + 1];
          const b = imgData.data[i + 2];
          // ITU-R BT.601 공식 기반 명도 계산
          const brightness = 0.299 * r + 0.587 * g + 0.114 * b;
          totalBrightness += brightness;
        }

        const avgBrightness = totalBrightness / (imgData.data.length / 4);
        
        // 2. 임계값(128) 기준으로 High-Key(밝음)와 Low-Key(어두움) 분류
        setIsLowKey(avgBrightness < 128);
      } catch (e) {
        console.warn("ContrastContainer: Cross-origin image block. Falling back to default dark overlay.", e);
        setIsLowKey(true);
      }
    };

    // [P1 수정] 이미지 로드 실패 시에 대한 예외 처리 누락 보완 (img.onerror)
    img.onerror = () => {
      console.warn("ContrastContainer: Image failed to load:", activeImageUrl);
      setIsLowKey(true); // 로드 실패 시 가독성 보존을 위해 어두운 레이아웃으로 안전 폴백
      setImageFailed(true);
    };
  }, [activeImageUrl]);

  // 배경 스타일 결정 (이미지가 실패했거나 없을 경우 fallbackColor를 적용)
  const containerStyle: React.CSSProperties = {
    ...style,
    backgroundImage: !imageFailed && activeImageUrl ? `url(${activeImageUrl})` : "none",
    backgroundColor: imageFailed || !activeImageUrl ? fallbackColor : "transparent",
  };

  return (
    <div
      className={`relative bg-cover bg-center overflow-hidden ${className}`}
      style={containerStyle}
      {...props}
    >
      {/* 3. 명도별 동적 가독성 오버레이 레이어 및 텍스트 대비 규칙 강제 */}
      <div 
        className={`absolute inset-0 transition-colors duration-300 ${
          isLowKey 
            ? "bg-black/40" // Low-Key: 어두운 오버레이
            : "bg-white/10" // High-Key: 투명/밝은 오버레이
        }`} 
      />
      
      {/* [P1 수정] 실제 텍스트 대비 색상 클래스를 children wrapper div 및 부모 z-10 레이어에 붙여 자식 요소들이 CSS 상속을 받도록 보정합니다. */}
      <div 
        className={`relative z-10 w-full h-full transition-colors duration-300 ${
          isLowKey 
            ? "text-slate-100" // Low-Key: 밝은 글자 강제
            : "text-slate-900" // High-Key: 어두운 글자 강제
        }`}
      >
        {children}
      </div>
    </div>
  );
};

export default ContrastContainer;
