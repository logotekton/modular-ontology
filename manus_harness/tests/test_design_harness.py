import sys
import os
import re
import shutil
import pytest

# 패키지 경로 추가
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from manus_harness.config import settings
from manus_harness.design.linter import design_linter
from manus_harness.tools.base import lint_and_write_frontend_code
from manus_harness.engine.app import active_sessions

BAD_CODE_SAMPLE = """
import React from "react";
import { Link } from "wouter";

export default function BadComponent() {
    // 안티패턴 1: 렌더링 페이즈 내 무한 루프 유발 쿼리 참조 (CRITICAL)
    const { data } = useQuery({
        queryKey: ["items"],
        queryFn: fetchItems,
        date: new Date() 
    });

    return (
        // 안티패턴 2: 양산형 중앙 정렬 (AI Slop) 및 고정 높이 지정 및 bg-card 대비 페어링 누락
        <div className="items-center justify-center min-h-screen flex flex-col h-[600px] bg-card">
            {/* 안티패턴 3: 중첩된 Link와 a 태그 (중간에 span이 끼어 있는 복잡한 구조도 검출 대상) */}
            <Link href="/dashboard">
                <span>
                    <a className="text-blue-500">Go to Dashboard</a>
                </span>
            </Link>

            {/* 안티패턴 4: style={{ height: 600 }} 인라인 리터럴 고정 높이 */}
            <div style={{ height: 600 }}>
                <p className="font-sans">이곳은 밋밋한 텍스트 영역입니다.</p>
            </div>
        </div>
    );
}
"""

TEST_DESIGN_SESSION = "test_design_session_v134"

@pytest.fixture(autouse=True)
def setup_and_teardown_design():
    """
    각 테스트마다 격리된 임시 세션 디렉토리를 완전 초기화 및 클린업합니다.
    [P1 파일 락 피드백 반영] rmtree() 호출 전, 활성화된 모든 세션의 SQLite 커넥션을 명시적으로 닫아 파일 잠금을 해제합니다.
    """
    for agent in list(active_sessions.values()):
        try:
            agent.close()
        except Exception:
            pass
    active_sessions.clear()

    session_dir = settings.get_session_dir(TEST_DESIGN_SESSION)
    if os.path.exists(session_dir):
        try:
            shutil.rmtree(session_dir)
        except Exception:
            pass
            
    os.makedirs(session_dir, exist_ok=True)
    
    yield
    
    # 테스트 완료 후 정리
    for agent in list(active_sessions.values()):
        try:
            agent.close()
        except Exception:
            pass
    active_sessions.clear()

    if os.path.exists(session_dir):
        try:
            shutil.rmtree(session_dir)
        except Exception:
            pass

def test_design_harness_lint():
    """
    [DESIGN TEST 1] 생성된 코드의 AI Slop 및 레이아웃 안티패턴 검출 검증
    """
    result = design_linter.lint_code(BAD_CODE_SAMPLE)
    
    # 검증 확인
    assert result["is_safe"] is False, "Critical 안티패턴이 감지되었으므로 is_safe는 False여야 합니다."
    assert any(f["rule"] == "nested_anchors" for f in result["findings"])
    assert any(f["rule"] == "unstable_render_reference" for f in result["findings"])
    assert any(f["rule"] == "missing_color_pairing" for f in result["findings"])
    assert any(f["rule"] == "overflow_risk_fixed_height" for f in result["findings"])
    assert any(f["rule"] == "monotonous_inter_font" for f in result["findings"]), "Inter 단일 폰트 사용 시 monotonous_inter_font 경고가 감지되어야 합니다."

def test_design_harness_auto_correction():
    """
    [DESIGN TEST 2] 감지된 안티패턴의 자동 자가 교정(Enforcement) 검증
    """
    corrected_code = design_linter.enforce_safe_design(BAD_CODE_SAMPLE)

    # 중첩 <a> 및 고정 h-[600px]가 사라졌는지 검증
    assert not re.search(r"(?<!-)h-\[\d+px\]", corrected_code), "순수 고정 height가 제거되어야 합니다."
    assert "min-h-[600px]" in corrected_code, "고정 height가 min-h-*로 안전 교정되어야 합니다."
    
    # style={{ height: 600 }} -> style={{ minHeight: 600 }} 교정 검증
    assert "minHeight: 600" in corrected_code, "style 인라인 height가 minHeight로 교정되어야 합니다."
    
    # 중첩 <a> 태그 교정 검증 (실제 <a\b 정규식으로 중첩된 <a> 태그 제거 완벽 단정)
    assert not re.search(r"<a\b", corrected_code), "중첩 <a> 태그가 완전히 제거되어야 합니다."

def test_lint_and_write_frontend_code_tool():
    """
    [DESIGN TEST 3] 에이전트 루프 도구 lint_and_write_frontend_code of 세션 워크스페이스 격리 및 탈출 차단 무결성 검증
    """
    session_dir = settings.get_session_dir(TEST_DESIGN_SESSION)
    
    # 1. CRITICAL 결함이 없는 코드 (자동 교정만으로 안전해질 수 있는 코드)
    safe_code_sample = """
    export default function SafeComp() {
        return (
            <div className="h-[400px] bg-card text-card-foreground">
                <p>Hello Safe Design</p>
            </div>
        );
    }
    """
    target_file = "safe_component.tsx"
    resolved_target = os.path.join(session_dir, target_file)
    if os.path.exists(resolved_target):
        os.remove(resolved_target)
        
    # 도구 호출 -> 정상 성공 및 파일 생성되어야 함
    msg = lint_and_write_frontend_code(TEST_DESIGN_SESSION, safe_code_sample, target_file)
    assert "written successfully" in msg
    assert os.path.exists(resolved_target)
    
    with open(resolved_target, "r", encoding="utf-8") as f:
        written_content = f.read()
    assert "min-h-[400px]" in written_content, "자동 교정이 반영되어 저장되어야 합니다."

    # 2. CRITICAL 결함이 남아 있는 코드 (unstable_render_reference 등)
    unsafe_code_sample = """
    export default function UnsafeComp() {
        const { data } = useQuery({ date: new Date() });
        return <div className="bg-card">Unsafe</div>;
    }
    """
    unsafe_file = "unsafe_component.tsx"
    resolved_unsafe = os.path.join(session_dir, unsafe_file)
    if os.path.exists(resolved_unsafe):
        os.remove(resolved_unsafe)
    
    # 도구 호출 -> 해결 불가능한 CRITICAL 결함으로 인해 RuntimeError(파일 쓰기 차단)가 발생해야 함
    with pytest.raises(RuntimeError) as exc_info:
        lint_and_write_frontend_code(TEST_DESIGN_SESSION, unsafe_code_sample, unsafe_file)
    
    assert "Unresolved Critical Findings" in str(exc_info.value)
    assert not os.path.exists(resolved_unsafe), "CRITICAL 결함 잔존 시 파일 쓰기가 차단되어야 합니다."

    # 3. 파일 쓰기 워크스페이스 탈출 시도 차단 검증 (Path Traversal)
    escape_file = "../../../etc/backdoor.tsx"
    with pytest.raises(PermissionError) as exc_info_escape:
        lint_and_write_frontend_code(TEST_DESIGN_SESSION, safe_code_sample, escape_file)
    
    assert "escapes the session workspace" in str(exc_info_escape.value)

def test_design_harness_monotonous_inter_font_no_false_positive():
    """
    [DESIGN TEST 4] [P3 피드백 반영] monotonous_inter_font 규칙이 변수명(InteractionLogger, InterStateType 등)에
    포함된 'Inter' 문자열을 폰트 사용으로 오해하여 오탐하지 않는지 검증합니다.
    """
    code_with_inter_vars = """
    import { InteractionLogger } from "./utils";
    
    export default function MyComponent() {
        const interState: InterStateType = "active";
        return (
            <div className="bg-background text-foreground">
                <p>여기는 font-sans나 Inter 폰트 정의가 전혀 없는 일반 텍스트 영역입니다.</p>
            </div>
        );
    }
    """
    result = design_linter.lint_code(code_with_inter_vars)
    
    # 폰트 사용(font-sans, 'Inter' 문자열 리터럴, fontFamily.*Inter 등)이 전혀 없으므로 monotonous_inter_font가 감지되지 않아야 함
    inter_font_findings = [f for f in result["findings"] if f["rule"] == "monotonous_inter_font"]
    assert len(inter_font_findings) == 0, "Variable names containing 'Inter' must not trigger monotonous_inter_font!"

def test_reconstruction_contract_detects_redesign_drift():
    """
    [DESIGN TEST 5] Image reconstruction contracts must block creative redesign drift.
    A light, table-dense MES source must not pass as a sparse dark card dashboard.
    """
    dark_redesign_code = """
    export default function MesDashboard() {
        const kpis = [
            { label: "OEE", value: "87%" },
            { label: "Throughput", value: "14000" }
        ];
        return (
            <main className="min-h-screen bg-slate-950 text-white">
                <aside className="sidebar">MES</aside>
                <section className="kpi-grid">
                    {kpis.map((item) => <article className="kpi-card">{item.label}</article>)}
                </section>
                <section className="cards">Work Orders</section>
            </main>
        );
    }
    """
    contract = {
        "theme_mode": "light",
        "required_regions": ["topbar", "sidebar", "kpi_strip", "process_flow", "production_table"],
        "required_text": ["OEE", "Throughput", "Defect Rate", "Downtime", "Work Orders"],
        "required_kpi_labels": ["OEE", "Throughput", "Defect Rate", "Downtime", "Work Orders"],
        "min_table_count": 2,
        "min_kpi_count": 5,
        "min_data_rows": 10,
        "strict_density": True
    }

    result = design_linter.lint_reconstruction_contract(dark_redesign_code, contract)
    rules = {finding["rule"] for finding in result["findings"]}

    assert result["is_safe"] is False
    assert "theme_mode_mismatch" in rules
    assert "required_region_missing" in rules
    assert "required_text_missing" in rules
    assert "minimum_table_count_missing" in rules
    assert "minimum_kpi_count_missing" in rules
    assert "minimum_data_rows_missing" in rules

def test_lint_and_write_frontend_code_blocks_contract_violations():
    """
    [DESIGN TEST 6] The write tool must enforce the reconstruction contract before saving.
    """
    contract = {
        "theme_mode": "light",
        "required_regions": ["topbar", "sidebar", "production_table"],
        "required_text": ["OEE", "Throughput", "Work Orders"],
        "min_table_count": 1,
        "min_kpi_count": 3
    }
    redesign_code = """
    export default function SparseDarkScreen() {
        return (
            <main className="min-h-screen bg-slate-950 text-white">
                <h1>MES</h1>
            </main>
        );
    }
    """

    with pytest.raises(RuntimeError) as exc_info:
        lint_and_write_frontend_code(
            TEST_DESIGN_SESSION,
            redesign_code,
            "contract_blocked.tsx",
            design_contract=contract
        )

    assert "theme_mode_mismatch" in str(exc_info.value)
    assert not os.path.exists(os.path.join(settings.get_session_dir(TEST_DESIGN_SESSION), "contract_blocked.tsx"))

def test_reconstruction_contract_counts_portal_arrays():
    """
    [DESIGN TEST 7] Search portal reconstructions should count dense portal data arrays,
    not only MES/table-style rows.
    """
    portal_code = """
    export default function SearchPortal() {
        const tabs = [{ label: "Search" }, { label: "News" }, { label: "Shopping" }];
        const sidebar = [{ label: "Home" }, { label: "Mail" }, { label: "Cafe" }];
        const keywords = [{ label: "AI SEO" }, { label: "Pastel UI" }, { label: "Local" }];
        const shortcuts = [{ label: "Map" }, { label: "Pay" }, { label: "Music" }];
        const shopping = [{ title: "Sneakers" }, { title: "Headphones" }, { title: "Bag" }];
        const places = [{ name: "Cafe" }, { name: "Studio" }, { name: "Pop-up" }];
        const feed = [{ title: "Trend" }, { title: "Campus" }, { title: "Creator" }];

        return (
            <main className="min-h-screen bg-white text-slate-900">
                <aside className="portal-sidebar">Mingle Search</aside>
                <nav className="top-tabs">{tabs.map((item) => <button>{item.label}</button>)}</nav>
                <section className="search-hero">
                    <input aria-label="search-bar" />
                    <div className="trending-keywords">{keywords.map((item) => <span>{item.label}</span>)}</div>
                </section>
                <section className="shortcut-grid">{shortcuts.map((item) => <button>{item.label}</button>)}</section>
                <section className="shopping-recommend">{shopping.map((item) => <article>{item.title}</article>)}</section>
                <section className="local-places">{places.map((item) => <article>{item.name}</article>)}</section>
                <section className="discovery-feed">{feed.map((item) => <article>{item.title}</article>)}</section>
                <section className="seo-insight">SEO Beta</section>
            </main>
        );
    }
    """
    contract = {
        "theme_mode": "light",
        "required_regions": [
            "portal-sidebar",
            "top-tabs",
            "search-hero",
            "search-bar",
            "trending-keywords",
            "shortcut-grid",
            "shopping-recommend",
            "local-places",
            "discovery-feed",
            "seo-insight"
        ],
        "required_text": ["Mingle Search", "SEO", "Beta"],
        "min_data_rows": 18,
        "strict_density": False
    }

    result = design_linter.lint_reconstruction_contract(portal_code, contract)

    assert result["is_safe"] is True
    assert result["findings_count"] == 0

def test_reconstruction_contract_blocks_missing_visual_quality_cues():
    """
    [DESIGN TEST 8] Image reconstruction contracts should enforce source assets,
    typography cues, and spacing rhythm when strict visual contracts are enabled.
    """
    sparse_code = """
    export default function PortalMock() {
        return (
            <main className="bg-white text-slate-900">
                <section className="search-hero">Mingle Search SEO Beta</section>
            </main>
        );
    }
    """
    contract = {
        "theme_mode": "light",
        "required_regions": ["search-hero"],
        "required_text": ["Mingle Search", "SEO", "Beta"],
        "required_assets": [
            {"label": "hero mascot crop", "aliases": ["assets/hero-mascot.png", "hero-mascot"]}
        ],
        "typography_contract": {
            "required_tokens": ["font-weight", "line-height"],
            "min_typography_tokens": 3
        },
        "spacing_contract": {
            "required_tokens": ["gap:", "padding:"],
            "min_spacing_tokens": 3
        },
        "strict_assets": True,
        "strict_typography": True,
        "strict_spacing": True
    }

    result = design_linter.lint_reconstruction_contract(sparse_code, contract)
    rules = {finding["rule"] for finding in result["findings"]}

    assert result["is_safe"] is False
    assert "required_asset_missing" in rules
    assert "typography_contract_missing" in rules
    assert "spacing_contract_missing" in rules

def test_visual_qa_report_blocks_low_similarity_and_bbox_drift():
    """
    [DESIGN TEST 9] Rendered visual QA should block low Pixelmatch similarity and
    source-vs-render region drift after Playwright screenshots are produced.
    """
    qa_report = {
        "visual_fidelity_score": 0.72,
        "pixel_diff_ratio": 0.19,
        "region_bboxes": {
            "search-hero": {"iou": 0.81},
            "shopping": {"iou": 0.42}
        }
    }
    contract = {
        "min_visual_fidelity_score": 0.82,
        "max_pixel_diff_ratio": 0.12,
        "required_region_bboxes": ["search-hero", "shopping", "seo-insight"],
        "min_region_iou": 0.65
    }

    result = design_linter.lint_visual_qa_report(qa_report, contract)
    rules = {finding["rule"] for finding in result["findings"]}

    assert result["is_safe"] is False
    assert "visual_fidelity_score_low" in rules
    assert "pixel_diff_too_high" in rules
    assert "region_bbox_mismatch" in rules
    assert "region_bbox_missing" in rules

def test_visual_qa_report_accepts_passing_metrics():
    """
    [DESIGN TEST 10] Good visual QA metrics should pass the post-render gate.
    """
    qa_report = {
        "visual_fidelity_score": 87,
        "pixel_diff_ratio": 0.08,
        "region_bboxes": [
            {"name": "search-hero", "iou": 0.82},
            {"name": "shopping", "iou": 0.75}
        ]
    }
    contract = {
        "min_visual_fidelity_score": 0.82,
        "max_pixel_diff_ratio": 0.12,
        "required_region_bboxes": ["search-hero", "shopping"],
        "min_region_iou": 0.65
    }

    result = design_linter.lint_visual_qa_report(qa_report, contract)

    assert result["is_safe"] is True
    assert result["findings_count"] == 0

def test_reconstruction_contract_counts_blog_arrays():
    """
    [DESIGN TEST 11] Blog reconstructions should count category, post, trend,
    mood, and challenge arrays as data density.
    """
    blog_code = """
    export default function BlogScreen() {
        const categories = [{ label: "Today" }, { label: "Life" }, { label: "Tech" }];
        const posts = [{ title: "Morning" }, { title: "Spring" }, { title: "Travel" }];
        const topics = [{ label: "Writing" }, { label: "Campus" }, { label: "Beauty" }];
        const moods = [["happy", 12], ["calm", 7], ["sad", 2]];
        const challenges = [{ title: "Weekly writing" }, { title: "Photo diary" }];

        return (
            <main className="bg-white text-slate-900">
                <aside className="blog-sidebar">BLOOMLOG</aside>
                <section className="search-hero">Blog hero</section>
                <section className="discovery-feed">{posts.map((item) => <article>{item.title}</article>)}</section>
                <section className="mood-calendar">Mood</section>
            </main>
        );
    }
    """
    contract = {
        "theme_mode": "light",
        "required_regions": ["blog-sidebar", "search-hero", "discovery-feed", "mood-calendar"],
        "required_text": ["BLOOMLOG"],
        "min_data_rows": 14,
        "strict_density": True
    }

    result = design_linter.lint_reconstruction_contract(blog_code, contract)

    assert result["is_safe"] is True
    assert result["findings_count"] == 0
