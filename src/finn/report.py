"""Reply email: verdict, summary, overview table, per-contract details with sources.

Builds an email-safe HTML body (table layout, inline styles, no scripts, no external
resources) and a plain-text alternative. All user-facing words come from
src/finn/texts/<lang>.toml, all colours and thresholds from config/report.toml.
The numbers come from the calculator; this module only formats them.
"""

from __future__ import annotations

import html
import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from finn.fmt import fmt_amount, fmt_num, fmt_pct
from finn.llm import JsonLLM
from finn.models import ContractCovenants, Result, RunReport, Source
from finn.render import load_texts, render_formula, render_stress
from finn.summary import SummaryOutcome, write_summary

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT_CONFIG = PROJECT_ROOT / "config" / "report.toml"


@lru_cache(maxsize=4)
def load_report_config(path: str | None = None) -> dict[str, Any]:
    """Load config/report.toml (path overridable via env REPORT_CONFIG)."""
    cfg_path = Path(path or os.environ.get("REPORT_CONFIG", "") or DEFAULT_REPORT_CONFIG)
    return tomllib.loads(cfg_path.read_text(encoding="utf-8"))


@dataclass
class ReplyContext:
    """Everything the reply needs besides the results."""

    lang: str = "de"
    salutation: str | None = None
    original_subject: str = ""
    focus_contracts: list[str] = field(default_factory=list)
    run_id: str = ""


@dataclass
class Reply:
    """A rendered reply, ready to be wrapped into a MIME message."""

    subject: str
    html: str
    text: str
    summary_meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _t(lang: str) -> dict[str, Any]:
    return load_texts(lang)


def reply_subject(original_subject: str, lang: str) -> str:
    """'Re: <subject>' without stacking prefixes."""
    prefix = str(_t(lang)["report"]["subject_prefix"])
    subj = (original_subject or "").strip()
    known = [prefix.strip().lower(), *load_report_config()["report"].get("existing_reply_prefixes", [])]
    if any(subj.lower().startswith(p.lower()) for p in known):
        return subj
    return f"{prefix}{subj}"


def _value_decimals(r: Result) -> int:
    if r.formula.rounding_decimals is not None:
        return r.formula.rounding_decimals
    return 2


def fmt_value(r: Result, value: float, lang: str) -> str:
    """Format a covenant value or limit in its unit."""
    if not r.value_defined and value == r.value_reported:
        denominator = r.stress.driver_quantity
        return str(_t(lang)["report"]["value_undefined"]).format(name=denominator)
    d = _value_decimals(r)
    if r.unit == "percent":
        return fmt_pct(value, decimals=d, lang=lang)
    return fmt_num(value, decimals=d, lang=lang)


def _limit_decimals(r: Result) -> int:
    """Rounding rule if the contract has one; otherwise as many decimals as the limit itself needs."""
    if r.formula.rounding_decimals is not None:
        return r.formula.rounding_decimals
    if r.unit != "percent":
        return 2
    text = f"{r.limit:.4f}".rstrip("0")
    decimals = len(text.split(".")[1]) if "." in text else 0
    return min(max(decimals, 1), 2)


def fmt_limit(r: Result, lang: str) -> str:
    texts = _t(lang)["report"]
    op = texts["op_ge"] if r.operator == ">=" else texts["op_le"]
    d = _limit_decimals(r)
    value = fmt_pct(r.limit, decimals=d, lang=lang) if r.unit == "percent" else fmt_num(r.limit, decimals=d, lang=lang)
    return f"{op} {value}"


def fmt_headroom(r: Result, lang: str) -> str:
    texts = _t(lang)["report"]
    if not r.value_defined:
        return "–"
    num = fmt_num(r.headroom, decimals=_value_decimals(r), lang=lang)
    key = "headroom_pp" if r.unit == "percent" else "headroom_ratio"
    return str(texts[key]).format(value=num)


def relative_headroom(r: Result) -> float:
    """Headroom as a share of the limit (0.023 = 2.3 % room left)."""
    if not r.value_defined:
        return 0.0
    if abs(r.limit) < 1e-12:
        return 0.0
    return r.headroom / abs(r.limit)


def status_of(r: Result, tight_threshold: float) -> str:
    if not r.met:
        return "breach"
    if relative_headroom(r) < tight_threshold:
        return "tight"
    return "ok"


def order_results(
    results: list[Result], focus_contracts: list[str], tight_threshold: float
) -> list[Result]:
    """Focus contracts first, then breaches, then by smallest relative headroom."""
    focus = [f.lower() for f in focus_contracts if f]

    def is_focus(r: Result) -> bool:
        hay = f"{r.contract_label} {r.contract_file}".lower()
        return any(f in hay for f in focus)

    return sorted(
        results,
        key=lambda r: (
            0 if is_focus(r) else 1,
            0 if not r.met else 1,
            relative_headroom(r),
        ),
    )


def short_label(label: str) -> str:
    """Contract label up to the first comma or parenthesis, for compact tables and sentences."""
    cut = len(label)
    for sep in (",", " ("):
        idx = label.find(sep)
        if idx > 0:
            cut = min(cut, idx)
    return label[:cut].strip() or label


def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _source_line(src: Source, lang: str) -> tuple[str, bool]:
    texts = _t(lang)["report"]
    line = str(texts["source_line"]).format(section=src.section, page=src.page, quote=src.quote)
    return line, src.verified is not False


def _plan_cell_line(report: RunReport, cell_id: str, unit: str, lang: str) -> str:
    texts = _t(lang)["report"]
    info = report.plan_cells.get(cell_id)
    if info is None:
        return cell_id
    value = fmt_amount(info.value, unit=unit, lang=lang, decimals=0 if float(info.value).is_integer() else 2)
    return str(texts["plan_cell_line"]).format(cell_id=cell_id, label=info.label, value=value)


# ---------------------------------------------------------------------------
# Verdict and summary facts
# ---------------------------------------------------------------------------


def build_verdict(results: list[Result], plan_meta: dict[str, Any], lang: str) -> list[str]:
    """One or two sentences, generated by code only."""
    texts = _t(lang)["report"]
    test_date = str(plan_meta.get("test_date", ""))
    n = len(results)
    if n == 0:
        return []
    breached = [r for r in results if not r.met]
    sentences: list[str] = []
    if breached:
        names = ", ".join(f"{r.covenant_name} ({short_label(r.contract_label)})" for r in breached)
        sentences.append(
            str(texts["verdict_breached"]).format(k=len(breached), n=n, test_date=test_date, names=names)
        )
    else:
        sentences.append(str(texts["verdict_all_met"]).format(n=n, test_date=test_date))
    met = [r for r in results if r.met]
    if met:
        tightest = min(met, key=relative_headroom)
        sentences.append(
            str(texts["verdict_tightest"]).format(
                name=tightest.covenant_name,
                contract=short_label(tightest.contract_label),
                headroom=fmt_headroom(tightest, lang),
            )
        )
    return sentences


def build_summary_facts(
    report: RunReport,
    ctx: ReplyContext,
    tight_threshold: float,
    unit: str,
    verdict: list[str] | None = None,
) -> dict[str, Any]:
    """Facts for the LLM summary: every number already rendered as a string."""
    lang = ctx.lang
    verdict = verdict or []
    covs = []
    for r in report.results:
        covs.append(
            {
                "id": r.covenant_id,
                "status_key": status_of(r, tight_threshold),
                "contract": r.contract_label,
                "covenant": r.covenant_name,
                "value": fmt_value(r, r.value_reported, lang),
                "limit": fmt_limit(r, lang),
                "headroom": fmt_headroom(r, lang),
                "status": _t(lang)["report"][f"status_{status_of(r, tight_threshold)}"],
                "stress": render_stress(r.stress, lang=lang, unit=unit),
                "plan_basis_note": r.plan_basis_note or "",
            }
        )
    return {
        "verdict_already_shown_above": verdict,
        "plan_year": str(report.plan_meta.get("plan_year", "")),
        "test_date": str(report.plan_meta.get("test_date", "")),
        "covenants_total": str(len(report.results)),
        "covenants_met": str(sum(1 for r in report.results if r.met)),
        "covenants": covs,
        "contracts_without_covenants": [c.contract_label for c in report.contracts_without_covenants],
        "focus_contracts": list(ctx.focus_contracts),
    }


def template_summary(report: RunReport, ctx: ReplyContext, tight_threshold: float, unit: str) -> list[str]:
    """Deterministic fallback summary from text templates."""
    lang = ctx.lang
    texts = _t(lang)["report"]
    out: list[str] = []
    breached = [r for r in report.results if not r.met]
    if breached:
        out.append(
            str(texts["summary_template_breach"]).format(
                plan_year=report.plan_meta.get("plan_year", ""),
                test_date=report.plan_meta.get("test_date", ""),
                names=", ".join(r.covenant_name for r in breached),
            )
        )
    met = [r for r in report.results if r.met]
    if met:
        tightest = min(met, key=relative_headroom)
        out.append(
            str(texts["summary_template_tight"]).format(
                name=tightest.covenant_name,
                contract=short_label(tightest.contract_label),
                stress=render_stress(tightest.stress, lang=lang, unit=unit),
            )
        )
    out.append(str(texts["summary_template_plan_basis"]))
    return out


# ---------------------------------------------------------------------------
# HTML building blocks (email-safe: tables + inline styles only)
# ---------------------------------------------------------------------------


def _badge(status: str, lang: str, colors: dict[str, str]) -> str:
    label = _t(lang)["report"][f"status_{status}"]
    color = colors[status]
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
        f'background:{color};color:#FFFFFF;font-size:12px;font-weight:600;'
        f'white-space:nowrap;">{_esc(label)}</span>'
    )


def _bar(r: Result, status: str, cfg: dict[str, Any]) -> str:
    colors = cfg["report"]["colors"]
    full_at = float(cfg["report"]["bar_full_at"])
    share = 0.0 if not r.met else max(0.0, min(relative_headroom(r) / full_at, 1.0))
    pct = max(2, int(round(share * 100))) if r.met else 0
    rest = 100 - pct
    fill = colors[status]
    cells = ""
    if pct > 0:
        cells += f'<td width="{pct}%" style="background:{fill};height:6px;font-size:0;line-height:0;">&nbsp;</td>'
    if rest > 0:
        cells += f'<td width="{rest}%" style="background:{colors["bar_track"]};height:6px;font-size:0;line-height:0;">&nbsp;</td>'
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse;margin-top:4px;"><tr>{cells}</tr></table>'
    )


def _p(text: str, colors: dict[str, str], *, size: int = 15, color_key: str = "text", extra: str = "") -> str:
    return (
        f'<p style="margin:0 0 10px 0;font-size:{size}px;line-height:1.5;'
        f'color:{colors[color_key]};{extra}">{_esc(text)}</p>'
    )


def _h(text: str, colors: dict[str, str], size: int = 17) -> str:
    return (
        f'<h2 style="margin:22px 0 10px 0;font-size:{size}px;line-height:1.3;'
        f'font-weight:600;color:{colors["text"]};">{_esc(text)}</h2>'
    )


def _overview_table(results: list[Result], lang: str, cfg: dict[str, Any]) -> str:
    """Four columns so it also fits a phone: metric (+contract), value (+limit), headroom (+bar), status."""
    texts = _t(lang)["report"]
    colors = cfg["report"]["colors"]
    tight = float(cfg["report"]["tight_threshold"])
    th = (
        f'style="text-align:left;padding:8px 6px;font-size:12px;font-weight:600;'
        f'color:{colors["muted"]};border-bottom:1px solid {colors["border"]};"'
    )
    th_r = th.replace("text-align:left", "text-align:right")
    head = (
        f"<tr><th {th}>{_esc(texts['col_covenant'])}</th>"
        f"<th {th_r}>{_esc(texts['col_value'])}</th>"
        f"<th {th}>{_esc(texts['col_headroom'])}</th><th {th}>{_esc(texts['col_status'])}</th></tr>"
    )
    td = (
        f'style="padding:9px 6px;font-size:14px;line-height:1.35;color:{colors["text"]};'
        f'border-bottom:1px solid {colors["border"]};vertical-align:top;hyphens:auto;"'
    )
    td_r = td.replace("vertical-align:top;", "vertical-align:top;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;")
    small = f'style="display:block;font-size:12px;color:{colors["muted"]};font-weight:400;"'
    rows = ""
    for r in results:
        st = status_of(r, tight)
        rows += (
            f'<tr><td {td} width="48%"><strong>{_esc(r.covenant_name)}</strong>'
            f"<span {small}>{_esc(short_label(r.contract_label))}</span></td>"
            f"<td {td_r}><strong>{_esc(fmt_value(r, r.value_reported, lang))}</strong>"
            f"<span {small}>{_esc(fmt_limit(r, lang))}</span></td>"
            f'<td {td} width="64"><span style="white-space:nowrap;font-variant-numeric:tabular-nums;">'
            f"{_esc(fmt_num(r.headroom, decimals=_value_decimals(r), lang=lang) if r.value_defined else '–')}</span>{_bar(r, st, cfg)}</td>"
            f"<td {td}>{_badge(st, lang, colors)}</td></tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse;">{head}{rows}</table>'
    )


def _kv_row(label: str, value_html: str, colors: dict[str, str]) -> str:
    return (
        f'<tr><td width="120" style="padding:5px 10px 5px 0;font-size:13px;color:{colors["muted"]};vertical-align:top;">'
        f"{_esc(label)}</td>"
        f'<td style="padding:5px 0;font-size:14px;line-height:1.45;color:{colors["text"]};vertical-align:top;">{value_html}</td></tr>'
    )


def _contract_block(
    contract_label: str,
    results: list[Result],
    report: RunReport,
    lang: str,
    cfg: dict[str, Any],
    unit: str,
) -> str:
    texts = _t(lang)["report"]
    colors = cfg["report"]["colors"]
    tight = float(cfg["report"]["tight_threshold"])
    inner = (
        f'<p style="margin:0 0 6px 0;font-size:15px;font-weight:600;color:{colors["text"]};">'
        f"{_esc(contract_label)}</p>"
    )
    for r in results:
        st = status_of(r, tight)
        sources_html = ""
        for src in r.contract_sources:
            line, ok = _source_line(src, lang)
            marker = "" if ok else (
                f' <span style="color:{colors["breach"]};font-weight:600;">{_esc(texts["unverified_marker"])}</span>'
            )
            sources_html += f'<div style="margin:0 0 4px 0;">{_esc(line)}{marker}</div>'
        cells_html = "".join(
            f'<div style="margin:0 0 2px 0;font-variant-numeric:tabular-nums;">{_esc(_plan_cell_line(report, c, unit, lang))}</div>'
            for c in r.plan_cells
        )
        rows = (
            _kv_row(texts["label_value"], f"<strong>{_esc(fmt_value(r, r.value_reported, lang))}</strong> &nbsp;{_badge(st, lang, colors)}", colors)
            + _kv_row(texts["label_limit"], _esc(fmt_limit(r, lang)), colors)
            + _kv_row(texts["label_headroom"], _esc(fmt_headroom(r, lang)), colors)
            + _kv_row(texts["label_stress"], _esc(render_stress(r.stress, lang=lang, unit=unit)), colors)
            + _kv_row(
                texts["label_formula"],
                f'<span style="font-family:Consolas,Menlo,monospace;font-size:13px;">{_esc(render_formula(r.formula, lang=lang, unit=unit))}</span>',
                colors,
            )
            + _kv_row(texts["label_contract_source"], sources_html, colors)
            + _kv_row(texts["label_plan_source"], cells_html, colors)
        )
        if r.plan_basis_note:
            rows += _kv_row(texts["label_plan_basis"], _esc(r.plan_basis_note), colors)
        inner += (
            f'<p style="margin:12px 0 4px 0;font-size:14px;font-weight:600;color:{colors["text"]};">'
            f"{_esc(r.covenant_name)}</p>"
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="border-collapse:collapse;">{rows}</table>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse;margin:0 0 14px 0;border:1px solid {colors["border"]};">'
        f'<tr><td style="padding:14px 16px;background:{colors["paper"]};">{inner}</td></tr></table>'
    )


def _no_covenant_block(cc: ContractCovenants, lang: str, colors: dict[str, str]) -> str:
    src_html = ""
    if cc.reason_source is not None:
        line, ok = _source_line(cc.reason_source, lang)
        src_html = f'<div style="margin-top:4px;font-size:13px;color:{colors["muted"]};">{_esc(line)}</div>'
    return (
        f'<div style="margin:0 0 10px 0;font-size:14px;line-height:1.5;color:{colors["text"]};">'
        f"<strong>{_esc(cc.contract_label)}</strong><br>{_esc(cc.reason_if_none or '')}{src_html}</div>"
    )


def _wrap_html(body: str, cfg: dict[str, Any], lang: str = "de") -> str:
    colors = cfg["report"]["colors"]
    width = int(cfg["report"]["max_width_px"])
    font = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
    return (
        f'<!DOCTYPE html><html lang="{_esc(lang)}"><head><meta charset="utf-8">'
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta name=\"color-scheme\" content=\"light\"></head>"
        f'<body style="margin:0;padding:0;background:{colors["bg"]};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{colors["bg"]};">'
        '<tr><td align="center" style="padding:20px 8px;">'
        f'<!--[if mso]><table role="presentation" width="{width}" align="center"><tr><td><![endif]-->'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;max-width:{width}px;background:{colors["paper"]};border:1px solid {colors["border"]};">'
        f'<tr><td style="padding:22px 18px 18px 18px;font-family:{font};color:{colors["text"]};">'
        f"{body}</td></tr></table><!--[if mso]></td></tr></table><![endif]--></td></tr></table></body></html>"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_reply(
    report: RunReport,
    ctx: ReplyContext,
    llm: JsonLLM | None = None,
    *,
    config_path: str | None = None,
) -> Reply:
    """Render the full result reply (HTML + text). The LLM only writes the summary sentences."""
    cfg = load_report_config(config_path)
    colors = cfg["report"]["colors"]
    tight = float(cfg["report"]["tight_threshold"])
    lang = ctx.lang
    texts = _t(lang)["report"]
    unit = str(report.plan_meta.get("amount_unit", ""))

    ordered = order_results(report.results, ctx.focus_contracts, tight)
    greeting = (
        str(texts["greeting_named"]).format(name=ctx.salutation)
        if ctx.salutation
        else str(texts["greeting"])
    )
    verdict = build_verdict(ordered, report.plan_meta, lang)
    if report.failed_contracts:
        verdict.insert(0, str(texts["verdict_incomplete"]).format(k=len(report.failed_contracts)))

    facts = build_summary_facts(report, ctx, tight, unit, verdict)
    summary_cfg = cfg["summary"]
    summary: SummaryOutcome = write_summary(
        facts,
        lang,
        llm,
        template_summary(report, ctx, tight, unit),
        max_sentences=int(summary_cfg["max_sentences"]),
        max_sentence_chars=int(summary_cfg["max_sentence_chars"]),
        small_counts_max=int(summary_cfg["allowed_small_counts_max"]),
        status_claims=dict(_t(lang).get("summary_claims", {})),
    )

    # Group results per contract, keeping the order of the first appearance.
    groups: dict[str, list[Result]] = {}
    for r in ordered:
        groups.setdefault(r.contract_label, []).append(r)

    # ---------------- HTML ----------------
    body = _p(greeting, colors)
    if verdict:
        body += (
            f'<p style="margin:0 0 12px 0;font-size:17px;line-height:1.45;font-weight:600;color:{colors["text"]};">'
            f"{_esc(' '.join(verdict))}</p>"
        )
    for s in summary.sentences:
        body += _p(s, colors)
    if ordered:
        body += _h(texts["overview_heading"], colors)
        body += _overview_table(ordered, lang, cfg)
        body += _h(texts["details_heading"], colors)
        for label, rs in groups.items():
            body += _contract_block(label, rs, report, lang, cfg, unit)
    if report.contracts_without_covenants:
        body += _h(texts["no_covenants_heading"], colors)
        for cc in report.contracts_without_covenants:
            body += _no_covenant_block(cc, lang, colors)
    warnings = [
        str(texts["contract_failed"]).format(contract=f["contract"], reason=f["reason"][:300])
        for f in report.failed_contracts
    ] + list(report.warnings) + [w for r in ordered for w in r.warnings]
    if warnings:
        body += _h(texts["warnings_heading"], colors)
        body += "".join(_p(w, colors, size=13, color_key="muted") for w in warnings)
    footer_run = str(texts["footer_run"]).format(
        run_id=ctx.run_id,
        file_name=report.plan_meta.get("file_name", ""),
        plan_year=report.plan_meta.get("plan_year", ""),
        unit=unit,
    )
    body += (
        f'<p style="margin:20px 0 0 0;font-size:14px;color:{colors["text"]};">{_esc(texts["signature"])}</p>'
        f'<p style="margin:14px 0 0 0;padding-top:10px;border-top:1px solid {colors["border"]};'
        f'font-size:12px;line-height:1.5;color:{colors["muted"]};">{_esc(texts["footer_method"])}<br>{_esc(footer_run)}</p>'
    )
    html_out = _wrap_html(body, cfg, lang)

    # ---------------- Plain text ----------------
    lines: list[str] = [greeting, ""]
    if verdict:
        lines += [" ".join(verdict), ""]
    lines += [" ".join(summary.sentences), ""]
    if ordered:
        lines += [str(texts["overview_heading"]).upper(), ""]
        for r in ordered:
            st = _t(lang)["report"][f"status_{status_of(r, tight)}"]
            lines.append(
                f"- {short_label(r.contract_label)} | {r.covenant_name}: {fmt_value(r, r.value_reported, lang)} "
                f"({texts['col_limit']} {fmt_limit(r, lang)}, {texts['col_headroom']} {fmt_headroom(r, lang)}) – {st}"
            )
        lines += ["", str(texts["details_heading"]).upper()]
        for label, rs in groups.items():
            lines += ["", label]
            for r in rs:
                lines.append(f"  {r.covenant_name}")
                lines.append(f"    {texts['label_stress']}: {render_stress(r.stress, lang=lang, unit=unit)}")
                lines.append(f"    {texts['label_formula']}: {render_formula(r.formula, lang=lang, unit=unit)}")
                lines.append(f"    {texts['label_contract_source']}:")
                for src in r.contract_sources:
                    line, ok = _source_line(src, lang)
                    lines.append(f"      - {line}" + ("" if ok else f" {texts['unverified_marker']}"))
                lines.append(f"    {texts['label_plan_source']}:")
                for c in r.plan_cells:
                    lines.append(f"      - {_plan_cell_line(report, c, unit, lang)}")
                if r.plan_basis_note:
                    lines.append(f"    {texts['label_plan_basis']}: {r.plan_basis_note}")
    if report.contracts_without_covenants:
        lines += ["", str(texts["no_covenants_heading"]).upper()]
        for cc in report.contracts_without_covenants:
            lines.append(f"- {cc.contract_label}: {cc.reason_if_none}")
    if warnings:
        lines += ["", str(texts["warnings_heading"]).upper()]
        lines += [f"- {w}" for w in warnings]
    lines += ["", str(texts["signature"]), "", "--", str(texts["footer_method"]), footer_run]
    text_out = "\n".join(lines).strip() + "\n"

    return Reply(
        subject=reply_subject(ctx.original_subject, lang),
        html=html_out,
        text=text_out,
        summary_meta={"source": summary.source, **summary.meta},
    )


def build_simple_reply(
    kind: str,
    ctx: ReplyContext,
    params: dict[str, Any] | None = None,
    questions: list[str] | None = None,
    *,
    config_path: str | None = None,
) -> Reply:
    """Short non-result replies: missing/wrong attachment, plan format error, unrelated, clarification, internal error."""
    cfg = load_report_config(config_path)
    colors = cfg["report"]["colors"]
    lang = ctx.lang
    texts = _t(lang)
    greeting = (
        str(texts["report"]["greeting_named"]).format(name=ctx.salutation)
        if ctx.salutation
        else str(texts["report"]["greeting"])
    )
    message = str(texts["replies"][kind]).format(**(params or {}))
    signature = str(texts["report"]["signature"])

    body = _p(greeting, colors) + _p(message, colors)
    lines = [greeting, "", message]
    if questions:
        body += "<ul style=\"margin:0 0 10px 18px;padding:0;\">" + "".join(
            f'<li style="font-size:15px;line-height:1.5;color:{colors["text"]};">{_esc(q)}</li>' for q in questions
        ) + "</ul>"
        lines += [f"- {q}" for q in questions]
    body += f'<p style="margin:18px 0 0 0;font-size:14px;color:{colors["text"]};">{_esc(signature)}</p>'
    lines += ["", signature]
    return Reply(
        subject=reply_subject(ctx.original_subject, lang),
        html=_wrap_html(body, cfg, lang),
        text="\n".join(lines) + "\n",
        summary_meta={"source": "template", "kind": kind},
    )
