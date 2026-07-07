import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
// Astryx 컴포넌트 스타일 + neutral 테마 토큰 (앱 자체 styles.css가 우선하도록 먼저 로드)
import "@astryxdesign/core/astryx.css";
import "@astryxdesign/theme-neutral/theme.css";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
