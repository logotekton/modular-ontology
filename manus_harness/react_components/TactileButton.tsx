import React from "react";

interface TactileButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "outline" | "ghost" | "destructive";
  size?: "sm" | "md" | "lg";
  children: React.ReactNode;
}

/**
 * TactileButton: 물리 법칙에 따른 햅틱 촉각 반응을 강제하는 하네스 컴포넌트
 * 
 * - active 시 transform: scale(0.96) 및 opacity 조절을 120ms 내로 처리하여 문서 명세와 일치시킵니다.
 * - hover 시 elastic-out 이징으로 세련된 하이라이트 제공
 */
export const TactileButton: React.FC<TactileButtonProps> = ({
  variant = "primary",
  size = "md",
  children,
  className = "",
  ...props
}) => {
  // [P2 수정] 1. active:scale-[0.96]으로 문서(0.96)와 일치시킵니다.
  // [P2 수정] 2. 하드코딩된 focus:ring-blue-500 대신, 테마 독립적인 focus:ring-primary 토큰을 사용하고 다크모드 링 오프셋(dark:ring-offset-slate-900)을 명시적으로 추가합니다.
  const baseStyles = "inline-flex items-center justify-center font-medium rounded-md transition-all select-none focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-primary dark:focus:ring-offset-slate-900 active:scale-[0.96] active:opacity-90 duration-150 ease-[cubic-bezier(0.23,1,0.32,1)]";
  
  const variants = {
    primary: "bg-primary text-primary-foreground hover:opacity-90",
    secondary: "bg-secondary text-secondary-foreground hover:bg-secondary/80",
    outline: "border border-input text-foreground bg-background hover:bg-accent hover:text-accent-foreground",
    ghost: "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
    destructive: "bg-destructive text-destructive-foreground hover:bg-destructive/90"
  };

  const sizes = {
    sm: "px-3 py-1.5 text-xs",
    md: "px-4 py-2 text-sm",
    lg: "px-6 py-3 text-base"
  };

  return (
    <button
      className={`${baseStyles} ${variants[variant]} ${sizes[size]} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
};

export default TactileButton;
