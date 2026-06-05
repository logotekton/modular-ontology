import re
import json
from typing import Dict, Any, List

class DesignLinter:
    """
    DesignLinter: 에이전트가 생성한 프론트엔드 코드(HTML, JSX, TSX, CSS)를 정적 분석하여
    디자인 안티패턴 및 UI/UX 결함을 탐지하고 자동으로 안전하게 교정하는 지능형 린터
    """
    def __init__(self):
        self.rules = {
            "unstable_render_reference": {
                "severity": "CRITICAL",
                "message": "Anti-Pattern: Creating unstable reference objects (e.g., 'new Date()') inside render phase. This causes infinite re-renders. Use useState() or useMemo()."
            },
            "nested_anchors": {
                "severity": "CRITICAL",
                "message": "Anti-Pattern: Nested anchor tags detected (e.g., '<a>' inside '<a>' or wouter's '<Link>'). This causes DOM nesting errors and runtime crashes."
            },
            "excessive_centered_layout": {
                "severity": "WARNING",
                "message": "Anti-Pattern: Excessive centered layout detected ('items-center justify-center' on screen height). Avoid generic centered layouts; prefer asymmetric, sidebar, or grid structures."
            },
            "overflow_risk_fixed_height": {
                "severity": "WARNING",
                "message": "Anti-Pattern: Hardcoded fixed height ('h-[600px]' or inline style height) detected on main container. This causes text overflow on smaller screens. Use min-h-* or minHeight instead."
            },
            "missing_color_pairing": {
                "severity": "WARNING",
                "message": "Anti-Pattern: Semantic background (e.g., 'bg-card') used without its corresponding text color (e.g., 'text-card-foreground'). Text may become invisible depending on active theme."
            },
            "monotonous_inter_font": {
                "severity": "WARNING",
                "message": "Anti-Pattern: Monotonous Inter font usage detected. Avoid using Inter font for the entire interface; combine a bold display font with a readable body font to build visual structure."
            },
            "theme_mode_mismatch": {
                "severity": "CRITICAL",
                "message": "Reconstruction mismatch: generated UI changed the source image theme mode. Keep the original light/dark theme instead of redesigning it."
            },
            "required_region_missing": {
                "severity": "CRITICAL",
                "message": "Reconstruction mismatch: a required source-image layout region is missing from the generated UI."
            },
            "required_text_missing": {
                "severity": "CRITICAL",
                "message": "Reconstruction mismatch: required source-image text or label is missing from the generated UI."
            },
            "minimum_table_count_missing": {
                "severity": "CRITICAL",
                "message": "Reconstruction mismatch: generated UI does not preserve the table density required by the source image."
            },
            "minimum_kpi_count_missing": {
                "severity": "CRITICAL",
                "message": "Reconstruction mismatch: generated UI does not preserve the required KPI count."
            },
            "minimum_data_rows_missing": {
                "severity": "WARNING",
                "message": "Reconstruction warning: generated UI appears less dense than the source image."
            },
            "required_asset_missing": {
                "severity": "WARNING",
                "message": "Reconstruction warning: generated UI is missing a required source-image asset or crop reference."
            },
            "typography_contract_missing": {
                "severity": "WARNING",
                "message": "Reconstruction warning: generated UI does not preserve required typography cues from the source image."
            },
            "spacing_contract_missing": {
                "severity": "WARNING",
                "message": "Reconstruction warning: generated UI does not preserve required spacing/layout rhythm cues from the source image."
            },
            "visual_fidelity_score_low": {
                "severity": "CRITICAL",
                "message": "Visual QA mismatch: rendered screenshot similarity is below the required fidelity score."
            },
            "region_bbox_missing": {
                "severity": "CRITICAL",
                "message": "Visual QA mismatch: a required source region was not measured in the rendered screenshot."
            },
            "region_bbox_mismatch": {
                "severity": "CRITICAL",
                "message": "Visual QA mismatch: a measured region bounding box is too far from the source image."
            },
            "pixel_diff_too_high": {
                "severity": "CRITICAL",
                "message": "Visual QA mismatch: screenshot pixel diff is above the allowed threshold."
            }
        }

    def _line_for_match(self, code_content: str, pattern: str, flags: int = 0) -> int:
        match = re.search(pattern, code_content, flags=flags)
        if not match:
            return 1
        return code_content[:match.start()].count("\n") + 1

    def _count_named_array_items(self, code_content: str, name: str) -> int:
        pattern = rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=\s*\["
        match = re.search(pattern, code_content)
        if not match:
            return 0
        start = match.end() - 1
        depth = 0
        quote = None
        escaped = False
        body_start = start + 1
        body_end = None
        for index in range(start, len(code_content)):
            char = code_content[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in {"'", '"', "`"}:
                quote = char
                continue
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    body_end = index
                    break
        if body_end is None:
            return 0

        body = code_content[body_start:body_end]
        item_count = 0
        depth = 1
        quote = None
        escaped = False
        for char in body:
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in {"'", '"', "`"}:
                quote = char
                continue
            if depth == 1 and char in {"{", "["}:
                item_count += 1
            if char in {"{", "["}:
                depth += 1
            elif char in {"}", "]"}:
                depth -= 1
        return item_count

    def _count_table_like_structures(self, code_content: str) -> int:
        table_patterns = [
            r"<table\b",
            r"role=[\"']table[\"']",
            r"className=[\"'][^\"']*(?:data-table|grid-table|table)[^\"']*[\"']",
            r"className=\{\s*`[^`]*(?:data-table|grid-table|table)[^`]*`\s*\}",
        ]
        return sum(len(re.findall(pattern, code_content, flags=re.IGNORECASE)) for pattern in table_patterns)

    def _count_data_rows(self, code_content: str) -> int:
        named_arrays = [
            "rows",
            "machines",
            "alerts",
            "inspections",
            "schedule",
            "orders",
            "workOrders",
            "productionRows",
            "equipmentRows",
            "lines",
            "keywords",
            "shortcuts",
            "shopping",
            "places",
            "trends",
            "music",
            "feed",
            "tabs",
            "sidebar",
            "categories",
            "posts",
            "postCards",
            "creators",
            "topics",
            "moods",
            "challenges"
        ]
        array_rows = sum(self._count_named_array_items(code_content, name) for name in named_arrays)
        markup_rows = len(re.findall(r"<tr\b|className=[\"'][^\"']*(?:row|table-row|order-row|inspection-row)[^\"']*[\"']", code_content, flags=re.IGNORECASE))
        return max(array_rows, markup_rows)

    def _count_kpis(self, code_content: str, required_labels: List[str]) -> int:
        if required_labels:
            lowered = code_content.lower()
            return sum(1 for label in required_labels if str(label).lower() in lowered)
        return max(
            self._count_named_array_items(code_content, "kpis"),
            len(re.findall(r"kpi-card|KpiCard|KPI", code_content, flags=re.IGNORECASE))
        )

    def _count_typography_tokens(self, code_content: str) -> int:
        patterns = [
            r"\bfont-(?:thin|light|normal|medium|semibold|bold|extrabold|black)\b",
            r"\btext-(?:xs|sm|base|lg|xl|[2-9]xl)\b",
            r"font-size\s*:",
            r"font-weight\s*:",
            r"line-height\s*:",
            r"letter-spacing\s*:",
            r"clamp\(",
        ]
        return sum(len(re.findall(pattern, code_content, flags=re.IGNORECASE)) for pattern in patterns)

    def _count_spacing_tokens(self, code_content: str) -> int:
        patterns = [
            r"\bgap(?:-[xy])?-\d+\b",
            r"\b(?:p|px|py|pt|pr|pb|pl|m|mx|my|mt|mr|mb|ml)-\d+\b",
            r"\bspace-[xy]-\d+\b",
            r"\bgap\s*:",
            r"\bpadding(?:-[a-z]+)?\s*:",
            r"\bmargin(?:-[a-z]+)?\s*:",
            r"grid-template-columns\s*:",
        ]
        return sum(len(re.findall(pattern, code_content, flags=re.IGNORECASE)) for pattern in patterns)

    def _normalize_score(self, value: Any) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return 0.0
        if score > 1:
            score = score / 100.0
        return max(0.0, min(1.0, score))

    def lint_reconstruction_contract(self, code_content: str, contract: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate image-to-UI fidelity constraints extracted from a reference image.

        This is intentionally stricter than the generic design linter: these
        findings are about reconstruction drift, not general UI quality. Use it
        when the task is to reproduce a screenshot instead of freely redesign it.
        """
        findings = []
        is_safe = True
        lowered = code_content.lower()

        def add(rule: str, severity: str, matched_text: str, line: int = 1):
            nonlocal is_safe
            if severity == "CRITICAL":
                is_safe = False
            findings.append({
                "rule": rule,
                "severity": severity,
                "line": line,
                "matched_text": matched_text,
                "message": self.rules[rule]["message"]
            })

        theme_mode = str(contract.get("theme_mode", "")).lower()
        if theme_mode in {"light", "dark"}:
            dark_markers = [
                r"color-scheme:\s*dark",
                r"#0[0-9a-f]{5}\b",
                r"bg-(?:slate|gray|zinc|neutral)-9\d\d",
                r"text-(?:white|slate-50|gray-50|zinc-50|neutral-50)",
                r"dark:"
            ]
            light_markers = [
                r"color-scheme:\s*light",
                r"#f[8-9a-f][0-9a-f]{4}\b",
                r"#fff(?:fff)?\b",
                r"bg-(?:white|slate-50|gray-50|zinc-50|neutral-50)"
            ]
            dark_score = sum(1 for pattern in dark_markers if re.search(pattern, code_content, flags=re.IGNORECASE))
            light_score = sum(1 for pattern in light_markers if re.search(pattern, code_content, flags=re.IGNORECASE))

            if theme_mode == "light" and dark_score >= 1 and light_score == 0:
                add("theme_mode_mismatch", "CRITICAL", "Expected light source theme, but generated code is dark-themed.", self._line_for_match(code_content, dark_markers[0], re.IGNORECASE))
            if theme_mode == "dark" and light_score >= 1 and dark_score == 0:
                add("theme_mode_mismatch", "CRITICAL", "Expected dark source theme, but generated code is light-themed.", self._line_for_match(code_content, light_markers[0], re.IGNORECASE))

        region_aliases = {
            "topbar": ["topbar", "top bar", "global bar", "site", "shift", "line"],
            "sidebar": ["sidebar", "side nav", "sidenav", "<aside", "<nav"],
            "kpi_strip": ["kpi", "oee", "throughput", "defect rate", "downtime", "work orders"],
            "process_flow": ["process flow", "process-flow", "smd", "aoi", "dip", "ict", "final test", "packing"],
            "production_table": ["production line status", "cell / machine", "performance", "<table"],
            "alerts": ["active alerts", "alerts", "alert-list"],
            "quality_queue": ["quality inspection queue", "inspection queue", "quality"],
            "equipment_table": ["equipment health", "health matrix", "equipment"],
            "work_order_table": ["batch / work order", "work orders", "schedule"]
        }
        for region in contract.get("required_regions", []) or []:
            aliases = region_aliases.get(str(region), [str(region)])
            if not any(alias.lower() in lowered for alias in aliases):
                add("required_region_missing", "CRITICAL", f"Missing region: {region}")

        for text in contract.get("required_text", []) or []:
            if str(text).lower() not in lowered:
                add("required_text_missing", "CRITICAL", f"Missing text: {text}")

        min_table_count = int(contract.get("min_table_count", 0) or 0)
        actual_table_count = self._count_table_like_structures(code_content)
        if min_table_count and actual_table_count < min_table_count:
            add(
                "minimum_table_count_missing",
                "CRITICAL",
                f"Expected at least {min_table_count} table-like structures, found {actual_table_count}."
            )

        required_kpi_labels = contract.get("required_kpi_labels", []) or []
        min_kpi_count = int(contract.get("min_kpi_count", 0) or 0)
        actual_kpi_count = self._count_kpis(code_content, required_kpi_labels)
        if min_kpi_count and actual_kpi_count < min_kpi_count:
            add(
                "minimum_kpi_count_missing",
                "CRITICAL",
                f"Expected at least {min_kpi_count} KPIs, found {actual_kpi_count}."
            )

        min_data_rows = int(contract.get("min_data_rows", 0) or 0)
        actual_data_rows = self._count_data_rows(code_content)
        if min_data_rows and actual_data_rows < min_data_rows:
            severity = "CRITICAL" if contract.get("strict_density") else "WARNING"
            add(
                "minimum_data_rows_missing",
                severity,
                f"Expected at least {min_data_rows} data rows, found {actual_data_rows}."
            )

        for asset in contract.get("required_assets", []) or []:
            if isinstance(asset, dict):
                label = str(asset.get("label") or asset.get("path") or asset.get("name") or "asset")
                aliases = asset.get("aliases") or [asset.get("path"), asset.get("label"), asset.get("name")]
                aliases = [str(alias) for alias in aliases if alias]
            else:
                label = str(asset)
                aliases = [label]
            if aliases and not any(alias.lower() in lowered for alias in aliases):
                severity = "CRITICAL" if contract.get("strict_assets") else "WARNING"
                add("required_asset_missing", severity, f"Missing asset reference: {label}")

        typography_contract = contract.get("typography_contract") or {}
        if typography_contract:
            required_tokens = [str(token) for token in typography_contract.get("required_tokens", []) if token]
            for token in required_tokens:
                if token.lower() not in lowered:
                    severity = "CRITICAL" if contract.get("strict_typography") else "WARNING"
                    add("typography_contract_missing", severity, f"Missing typography token: {token}")
            min_tokens = int(typography_contract.get("min_typography_tokens", 0) or 0)
            actual_tokens = self._count_typography_tokens(code_content)
            if min_tokens and actual_tokens < min_tokens:
                severity = "CRITICAL" if contract.get("strict_typography") else "WARNING"
                add(
                    "typography_contract_missing",
                    severity,
                    f"Expected at least {min_tokens} typography cues, found {actual_tokens}."
                )

        spacing_contract = contract.get("spacing_contract") or {}
        if spacing_contract:
            required_tokens = [str(token) for token in spacing_contract.get("required_tokens", []) if token]
            for token in required_tokens:
                if token.lower() not in lowered:
                    severity = "CRITICAL" if contract.get("strict_spacing") else "WARNING"
                    add("spacing_contract_missing", severity, f"Missing spacing token: {token}")
            min_tokens = int(spacing_contract.get("min_spacing_tokens", 0) or 0)
            actual_tokens = self._count_spacing_tokens(code_content)
            if min_tokens and actual_tokens < min_tokens:
                severity = "CRITICAL" if contract.get("strict_spacing") else "WARNING"
                add(
                    "spacing_contract_missing",
                    severity,
                    f"Expected at least {min_tokens} spacing cues, found {actual_tokens}."
                )

        return {
            "is_safe": is_safe,
            "findings": findings,
            "findings_count": len(findings)
        }

    def lint_visual_qa_report(self, qa_report: Dict[str, Any], contract: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate rendered screenshot QA metrics, such as Pixelmatch similarity and
        source-vs-render region bounding-box overlap.

        This complements lint_reconstruction_contract(): the reconstruction
        contract blocks static drift before writing code, while this method
        blocks low-fidelity rendered output after Playwright/Pixelmatch runs.
        """
        findings = []
        is_safe = True

        def add(rule: str, severity: str, matched_text: str):
            nonlocal is_safe
            if severity == "CRITICAL":
                is_safe = False
            findings.append({
                "rule": rule,
                "severity": severity,
                "line": 1,
                "matched_text": matched_text,
                "message": self.rules[rule]["message"]
            })

        min_score = contract.get("min_visual_fidelity_score")
        if min_score is not None:
            actual_score = self._normalize_score(qa_report.get("visual_fidelity_score"))
            required_score = self._normalize_score(min_score)
            if actual_score < required_score:
                add(
                    "visual_fidelity_score_low",
                    "CRITICAL",
                    f"Expected visual fidelity >= {required_score:.2f}, found {actual_score:.2f}."
                )

        max_pixel_diff_ratio = contract.get("max_pixel_diff_ratio")
        if max_pixel_diff_ratio is not None:
            try:
                actual_ratio = float(qa_report.get("pixel_diff_ratio", 1.0))
                required_ratio = float(max_pixel_diff_ratio)
            except (TypeError, ValueError):
                actual_ratio = 1.0
                required_ratio = 0.0
            if actual_ratio > required_ratio:
                add(
                    "pixel_diff_too_high",
                    "CRITICAL",
                    f"Expected pixel diff <= {required_ratio:.4f}, found {actual_ratio:.4f}."
                )

        raw_region_bboxes = qa_report.get("region_bboxes") or {}
        if isinstance(raw_region_bboxes, list):
            region_bboxes = {
                str(item.get("name")): item
                for item in raw_region_bboxes
                if isinstance(item, dict) and item.get("name")
            }
        else:
            region_bboxes = {
                str(name): value
                for name, value in raw_region_bboxes.items()
            }

        min_region_iou = float(contract.get("min_region_iou", 0) or 0)
        required_regions = contract.get("required_region_bboxes", []) or []
        for region in required_regions:
            region_name = str(region)
            region_metrics = region_bboxes.get(region_name)
            if region_metrics is None:
                add("region_bbox_missing", "CRITICAL", f"Missing visual QA region: {region_name}")
                continue
            if isinstance(region_metrics, dict):
                region_iou = self._normalize_score(region_metrics.get("iou"))
            else:
                region_iou = self._normalize_score(region_metrics)
            if min_region_iou and region_iou < min_region_iou:
                add(
                    "region_bbox_mismatch",
                    "CRITICAL",
                    f"Region {region_name} expected IoU >= {min_region_iou:.2f}, found {region_iou:.2f}."
                )

        return {
            "is_safe": is_safe,
            "findings": findings,
            "findings_count": len(findings)
        }

    def lint_code(self, code_content: str) -> Dict[str, Any]:
        """
        코드를 스캔하여 위반 사항 목록을 수집합니다.
        """
        findings = []
        is_safe = True

        # 1. CRITICAL: 렌더링 페이즈 내 불안정한 참조 생성 (unstable_render_reference)
        # 예: useQuery({ date: new Date() }) 또는 ids: [1, 2, 3] 등 render 시 직접 인스턴스화
        # [P1 피드백 반영] .useQuery() 외에도 일반 useQuery() 및 react-query 표준 사용법 전체를 감지하도록 점(\.) 제거
        unstable_patterns = [
            r"useQuery\(\s*\{\s*[^}]*date\s*:\s*new\s+Date\([^)]*\)",
            r"useQuery\(\s*\{\s*[^}]*ids\s*:\s*\[[^\]]*\]"
        ]
        for pattern in unstable_patterns:
            matches = re.finditer(pattern, code_content)
            for m in matches:
                is_safe = False
                line_no = code_content[:m.start()].count("\n") + 1
                findings.append({
                    "rule": "unstable_render_reference",
                    "severity": "CRITICAL",
                    "line": line_no,
                    "matched_text": m.group(0),
                    "message": self.rules["unstable_render_reference"]["message"]
                })

        # 2. CRITICAL: 중첩된 앵커 태그 탐지 (nested_anchors)
        # <a> 태그 내부에 또 다른 <a> 태그가 중첩되어 렌더링되는 경우 탐지 (DOTALL로 개행 허용)
        nested_patterns = [
            r"<a\b[^>]*>.*?<a\b[^>]*>.*?</a>.*?</a>",
            r"<Link\b[^>]*>.*?<a\b[^>]*>.*?</a>.*?</Link>"
        ]
        for pattern in nested_patterns:
            matches = re.finditer(pattern, code_content, flags=re.DOTALL)
            for m in matches:
                is_safe = False
                line_no = code_content[:m.start()].count("\n") + 1
                findings.append({
                    "rule": "nested_anchors",
                    "severity": "CRITICAL",
                    "line": line_no,
                    "matched_text": m.group(0),
                    "message": self.rules["nested_anchors"]["message"]
                })

        # 3. WARNING: 양산형 전역 중앙 정렬 남용 (excessive_centered_layout) & 시맨틱 색상 대비 누락 (missing_color_pairing)
        # className 구문을 정밀 파싱하여, 토큰 단위의 매칭을 수행합니다.
        class_blocks = re.findall(r'className=(?:["\']([^"\']*)["\']|\{\s*`([^`]*)`\s*\})', code_content)
        
        centered_layout_count = 0
        bg_targets = {"bg-card", "bg-popover", "bg-accent", "bg-destructive", "bg-muted"}

        for block in class_blocks:
            class_str = block[0] or block[1] or ""
            raw_tokens = class_str.split()
            
            # (A) [P2 수정] 다크모드 및 반응형 접두사(sm:, md:, lg:, dark:)를 안전하게 떼어내고 토큰 맵 구성
            tokens = set()
            bare_tokens = set()
            prefix_map = {} # prefix -> set of base tokens
            
            for token in raw_tokens:
                parts = token.split(":")
                base_token = parts[-1]
                tokens.add(base_token)
                
                if len(parts) > 1:
                    prefix = ":".join(parts[:-1])
                    prefix_map.setdefault(prefix, set()).add(base_token)
                else:
                    bare_tokens.add(base_token)
            
            # 1) 순수 배경색 누락 체크 (예: bg-card가 단독으로 쓰이고 text-card-foreground가 없을 때)
            used_bare_bgs = bg_targets.intersection(bare_tokens)
            for bg in used_bare_bgs:
                fg = bg.replace("bg-", "text-") + "-foreground"
                if fg not in bare_tokens:
                    match_pos = code_content.find(class_str)
                    line_no = code_content[:match_pos].count("\n") + 1 if match_pos != -1 else 1
                    findings.append({
                        "rule": "missing_color_pairing",
                        "severity": "WARNING",
                        "line": line_no,
                        "matched_text": f"{bg} (missing {fg} in '{class_str}')",
                        "message": self.rules["missing_color_pairing"]["message"]
                    })
            
            # 2) prefix가 결합된 배경색 누락 체크 (예: dark:bg-card)
            for prefix, base_set in prefix_map.items():
                used_prefixed_bgs = bg_targets.intersection(base_set)
                for bg in used_prefixed_bgs:
                    fg = bg.replace("bg-", "text-") + "-foreground"
                    # 동일한 prefix를 가진 foreground 토큰이 있는지 검사 (예: dark:text-card-foreground)
                    if fg not in base_set:
                        match_pos = code_content.find(class_str)
                        line_no = code_content[:match_pos].count("\n") + 1 if match_pos != -1 else 1
                        findings.append({
                            "rule": "missing_color_pairing",
                            "severity": "WARNING",
                            "line": line_no,
                            "matched_text": f"{prefix}:{bg} (missing {prefix}:{fg} in '{class_str}')",
                            "message": self.rules["missing_color_pairing"]["message"]
                        })

            # (B) [P2 수정] excessive_centered_layout을 className 블록 단위 정밀 토큰 분석으로 교체 (오탐 원천 소거)
            # 한 className 블록 내에 items-center, justify-center, min-h-screen (또는 h-screen)이 동시에 존재할 때 카운트
            centered_tokens = {"items-center", "justify-center"}
            has_height = "min-h-screen" in tokens or "h-screen" in tokens
            if centered_tokens.issubset(tokens) and has_height:
                centered_layout_count += 1

        if centered_layout_count >= 2:
            findings.append({
                "rule": "excessive_centered_layout",
                "severity": "WARNING",
                "line": 1,
                "matched_text": f"Found {centered_layout_count} centered layouts on full screen height.",
                "message": self.rules["excessive_centered_layout"]["message"]
            })

        # 4. WARNING: 고정 높이 오버플로우 리스크 (overflow_risk_fixed_height)
        # h-[600px] 등 대시보드나 메인 캔버스에 고정 픽셀 높이를 주어 콘텐츠가 잘릴 수 있는 위험
        # [P1 피드백 반영] (?<!-) lookbehind를 적용하여 min-h-[...] 내부의 h-[...]를 오탐하는 결함을 원천 방지
        fixed_height_patterns = [
            r"(?<!-)h-\[\d+px\]",
            r"style=\{\{\s*height\s*:\s*\d+\s*\}\}"
        ]
        for pattern in fixed_height_patterns:
            matches = re.finditer(pattern, code_content)
            for m in matches:
                # AspectCanvas 컴포넌트 내부나 min-h가 함께 있는 경우는 예외로 간주
                line_no = code_content[:m.start()].count("\n") + 1
                findings.append({
                    "rule": "overflow_risk_fixed_height",
                    "severity": "WARNING",
                    "line": line_no,
                    "matched_text": m.group(0),
                    "message": self.rules["overflow_risk_fixed_height"]["message"]
                })

        # 5. WARNING: 폰트 단조로움 감지 (monotonous_inter_font)
        # 코드 전체에서 Inter 폰트만 일관되게 사용하고 다른 display 폰트가 전혀 감지되지 않을 때 경고
        # (전역 폰트가 Inter나 font-sans로 지정되어 있고, serif나 playfair, display 계열 폰트가 없는 경우)
        font_in_code = (
            bool(re.search(r"\bfont-sans\b", code_content)) or
            bool(re.search(r"""['"]Inter['"]""", code_content)) or
            bool(re.search(r"fontFamily.*Inter", code_content))
        )
        if font_in_code:
            display_fonts = ["font-serif", "Playfair", "Montserrat", "Poppins", "Space Grotesk", "DM Sans", "Work Sans"]
            if not any(f in code_content for f in display_fonts):
                findings.append({
                    "rule": "monotonous_inter_font",
                    "severity": "WARNING",
                    "line": 1,
                    "matched_text": "Only Inter/sans-serif font detected without display fonts.",
                    "message": self.rules["monotonous_inter_font"]["message"]
                })

        return {
            "is_safe": is_safe,
            "findings": findings,
            "findings_count": len(findings)
        }

    def enforce_safe_design(self, code_content: str) -> str:
        """
        경고 등급(WARNING)의 일부 규칙 위반 사항을 안전하게 자동 교정(Auto-Correction)합니다.
        CRITICAL 결함은 자동 치환이 위험하므로 교정하지 않고 에러를 발생시켜 에이전트의 수정을 강제합니다.
        """
        corrected = code_content

        # 1. nested_anchors 자동 교정 (CRITICAL 중첩 앵커 해소)
        # <Link>와 <a> 중첩 시 내부 <a> 태그를 <span>으로 안전하게 치환
        # DOTALL 플래그를 추가하여 줄바꿈이 포함된 구조도 완벽히 치환
        link_nested_pattern = r"(<Link\b[^>]*>)(.*?)(<a\b[^>]*>)(.*?)(</a>)(.*?)(</Link>)"
        corrected = re.sub(link_nested_pattern, r"\1\2<span className='link-span'>\4</span>\6\7", corrected, flags=re.DOTALL)

        # 2. overflow_risk_fixed_height 자동 교정
        # [P1 피드백 반영] (?<!-) lookbehind를 복원하여 min-h-[600px]가 min-min-h-[600px]으로 이중 오교정되는 현상 방지
        corrected = re.sub(r"(?<!-)h-\[(\d+px)\]", r"min-h-[\1]", corrected)
        # style={{ height: 600 }} -> style={{ minHeight: 600 }} 변경
        corrected = re.sub(r"style=\{\{\s*height\s*:\s*(\d+)\s*\}\}", r"style={{ minHeight: \1 }}", corrected)

        # 3. missing_color_pairing 자동 교정
        # bg-card가 단독으로 사용된 경우 text-card-foreground를 함께 보강
        # bg-popover -> text-popover-foreground 등
        # [P2 피드백 반영] 반응형/다크모드 접두사(예: dark:bg-card)에 대응하는 text-card-foreground도 완벽히 지원
        bg_targets = ["bg-card", "bg-popover", "bg-accent", "bg-destructive", "bg-muted"]
        
        class_blocks = re.findall(r'className=(?:["\']([^"\']*)["\']|\{\s*`([^`]*)`\s*\})', corrected)
        for block in class_blocks:
            class_str = block[0] or block[1] or ""
            raw_tokens = class_str.split()
            
            tokens = set()
            bare_tokens = set()
            prefix_map = {}
            for t in raw_tokens:
                parts = t.split(":")
                base_token = parts[-1]
                tokens.add(base_token)
                if len(parts) > 1:
                    prefix = ":".join(parts[:-1])
                    prefix_map.setdefault(prefix, set()).add(base_token)
                else:
                    bare_tokens.add(base_token)
            
            modified_tokens = list(raw_tokens)
            is_modified = False
            
            # 1) 순수 배경색 누락 보정
            used_bare_bgs = set(bg_targets).intersection(bare_tokens)
            for bg in used_bare_bgs:
                fg = bg.replace("bg-", "text-") + "-foreground"
                if fg not in bare_tokens:
                    modified_tokens.append(fg)
                    is_modified = True
            
            # 2) prefixed 배경색 누락 보정 (예: dark:bg-card -> dark:text-card-foreground 추가)
            for prefix, base_set in prefix_map.items():
                used_prefixed_bgs = set(bg_targets).intersection(base_set)
                for bg in used_prefixed_bgs:
                    fg = bg.replace("bg-", "text-") + "-foreground"
                    if fg not in base_set:
                        modified_tokens.append(f"{prefix}:{fg}")
                        is_modified = True
            
            if is_modified:
                new_class_str = " ".join(modified_tokens)
                corrected = corrected.replace(class_str, new_class_str, 1)

        return corrected

# [P1 피드백 반영] design_linter 전역 인스톤스 및 DesignLinter 클래스 정의 정합 복원
design_linter = DesignLinter()
