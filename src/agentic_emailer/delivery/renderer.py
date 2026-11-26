"""Responsive HTML, plain-text, and deterministic fallback rendering for DigestDrafts."""

from __future__ import annotations

import html
from collections import defaultdict
from datetime import UTC, datetime

from agentic_emailer.ingestion.rss import is_valid_http_url
from agentic_emailer.models import (
    Citation,
    DigestDraft,
    DigestHighlight,
    RawArticle,
    RecipientProfile,
)


def _sanitize_header_value(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def _safe_href(url: str) -> str | None:
    cleaned = url.strip()
    if not is_valid_http_url(cleaned):
        return None
    return html.escape(cleaned, quote=True)


def _group_highlights_by_topic(
    highlights: list[DigestHighlight],
) -> list[tuple[str, list[DigestHighlight]]]:
    grouped: dict[str, list[DigestHighlight]] = defaultdict(list)
    order: list[str] = []
    for item in highlights:
        topic_key = item.topic.strip() or "General"
        if topic_key not in grouped:
            order.append(topic_key)
        grouped[topic_key].append(item)
    return [(topic, grouped[topic]) for topic in order]


def render_subject(profile: RecipientProfile, draft: DigestDraft) -> str:
    """Build a concise, header-safe email subject line."""
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    prefix = profile.subject_prefix.strip() or "[Daily Digest]"
    highlight_count = len(draft.highlights)
    if highlight_count == 0:
        subject = f"{prefix} {date_str} — Quiet day (0 new updates)"
    else:
        topic_count = max(1, len({h.topic.strip().lower() for h in draft.highlights if h.topic}))
        label = "highlight" if highlight_count == 1 else "highlights"
        topic_label = "topic" if topic_count == 1 else "topics"
        subject = (
            f"{prefix} {date_str} — {highlight_count} {label} "
            f"across {topic_count} {topic_label}"
        )
    return _sanitize_header_value(subject)


def render_plain_text(draft: DigestDraft) -> str:
    """Render a clean Markdown-like plain-text representation of DigestDraft."""
    lines: list[str] = [
        draft.title,
        "=" * min(len(draft.title), 72),
        "",
        "EXECUTIVE SUMMARY",
        draft.executive_summary.strip(),
        "",
    ]

    if not draft.highlights:
        lines.append("No new stories met your criteria in this window.")
    else:
        for topic, items in _group_highlights_by_topic(draft.highlights):
            lines.append(f"## {topic}")
            lines.append("")
            for item in items:
                badge = " [DEVELOPING UPDATE]" if item.story_status == "developing_update" else ""
                lines.append(f"- {item.title}{badge}")
                lines.append(f"  {item.summary.strip()}")
                lines.append(f"  Why it matters: {item.why_it_matters.strip()}")
                if item.citations:
                    sources = ", ".join(f"{c.source_name} ({c.url})" for c in item.citations)
                    lines.append(f"  Sources: {sources}")
                lines.append("")

    if draft.notable_trends:
        lines.append("NOTABLE TRENDS")
        for trend in draft.notable_trends:
            lines.append(f"* {trend.strip()}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_html(draft: DigestDraft) -> str:
    """Render a responsive, inline-CSS HTML email with strict HTML escaping."""
    escaped_title = html.escape(draft.title, quote=True)
    escaped_exec = html.escape(draft.executive_summary.strip(), quote=True)
    date_label = datetime.now(UTC).strftime("%B %d, %Y")

    topic_sections: list[str] = []
    if not draft.highlights:
        topic_sections.append(
            "<p style='color:#475569;font-size:15px;line-height:1.6;'>"
            "No new stories were found for your configured topics in this window."
            "</p>"
        )
    else:
        for topic, items in _group_highlights_by_topic(draft.highlights):
            escaped_topic = html.escape(topic, quote=True)
            cards: list[str] = []
            for item in items:
                escaped_item_title = html.escape(item.title, quote=True)
                escaped_summary = html.escape(item.summary.strip(), quote=True)
                escaped_why = html.escape(item.why_it_matters.strip(), quote=True)

                badge_html = ""
                if item.story_status == "developing_update":
                    badge_html = (
                        "<span style='display:inline-block;background-color:#FEF3C7;"
                        "color:#92400E;font-size:11px;font-weight:700;letter-spacing:0.04em;"
                        "padding:3px 8px;border-radius:9999px;margin-bottom:8px;'>"
                        "DEVELOPING UPDATE</span><br/>"
                    )

                citation_pills: list[str] = []
                for cit in item.citations:
                    source_text = html.escape(cit.source_name, quote=True)
                    safe_url = _safe_href(cit.url)
                    if safe_url:
                        citation_pills.append(
                            f"<a href='{safe_url}' target='_blank' rel='noopener noreferrer' "
                            "style='display:inline-block;background-color:#EFF6FF;color:#1D4ED8;"
                            "text-decoration:none;font-size:12px;font-weight:600;"
                            "padding:4px 10px;border-radius:9999px;margin:4px 6px 0 0;"
                            "border:1px solid #BFDBFE;'>"
                            f"{source_text} &#8599;</a>"
                        )
                    else:
                        citation_pills.append(
                            "<span style='display:inline-block;background-color:#F1F5F9;"
                            "color:#475569;font-size:12px;padding:4px 10px;border-radius:9999px;"
                            f"margin:4px 6px 0 0;'>{source_text}</span>"
                        )

                citations_html = "".join(citation_pills)
                cards.append(
                    "<div style='background-color:#FFFFFF;border:1px solid #E2E8F0;"
                    "border-radius:10px;padding:16px 18px;margin-bottom:14px;'>"
                    f"{badge_html}"
                    f"<h3 style='margin:0 0 8px 0;font-size:16px;line-height:1.35;"
                    f"color:#0F172A;'>{escaped_item_title}</h3>"
                    f"<p style='margin:0 0 10px 0;font-size:14px;line-height:1.6;color:#334155;'>"
                    f"{escaped_summary}</p>"
                    "<div style='background-color:#F8FAFC;border-left:3px solid #2563EB;"
                    "padding:8px 12px;margin:0 0 10px 0;border-radius:0 6px 6px 0;'>"
                    "<span style='font-size:12px;font-weight:700;color:#1E40AF;"
                    "text-transform:uppercase;letter-spacing:0.03em;'>Why it matters: </span>"
                    f"<span style='font-size:13px;line-height:1.5;color:#1E293B;'>"
                    f"{escaped_why}</span></div>"
                    f"<div style='margin-top:6px;'>{citations_html}</div>"
                    "</div>"
                )

            topic_sections.append(
                "<div style='margin:24px 0 8px 0;'>"
                "<h2 style='margin:0 0 12px 0;font-size:13px;font-weight:700;"
                "text-transform:uppercase;letter-spacing:0.08em;color:#475569;"
                f"border-bottom:1px solid #E2E8F0;padding-bottom:6px;'>{escaped_topic}</h2>"
                f"{''.join(cards)}"
                "</div>"
            )

    trends_html = ""
    if draft.notable_trends:
        trend_items = "".join(
            "<li style='margin-bottom:6px;font-size:14px;line-height:1.5;color:#334155;'>"
            f"{html.escape(trend.strip(), quote=True)}</li>"
            for trend in draft.notable_trends
            if trend.strip()
        )
        if trend_items:
            trends_html = (
                "<div style='background-color:#F8FAFC;border:1px solid #E2E8F0;"
                "border-radius:10px;padding:16px 18px;margin-top:24px;'>"
                "<h2 style='margin:0 0 10px 0;font-size:13px;font-weight:700;"
                "text-transform:uppercase;letter-spacing:0.08em;color:#0F172A;'>"
                "Notable Trends</h2>"
                f"<ul style='margin:0;padding-left:18px;'>{trend_items}</ul>"
                "</div>"
            )

    return (
        "<!DOCTYPE html>"
        "<html lang='en'><head><meta charset='utf-8'/>"
        "<meta name='viewport' content='width=device-width,initial-scale=1.0'/>"
        f"<title>{escaped_title}</title></head>"
        "<body style='margin:0;padding:0;background-color:#F1F5F9;"
        "font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",Roboto,Helvetica,Arial,"
        "sans-serif;color:#0F172A;'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' "
        "style='background-color:#F1F5F9;padding:24px 12px;'>"
        "<tr><td align='center'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' "
        "style='max-width:680px;background-color:#FFFFFF;border-radius:14px;"
        "overflow:hidden;border:1px solid #E2E8F0;'>"
        "<tr><td style='background-color:#0F172A;padding:24px 28px;'>"
        f"<p style='margin:0 0 6px 0;font-size:12px;letter-spacing:0.08em;"
        f"text-transform:uppercase;color:#94A3B8;'>{date_label}</p>"
        f"<h1 style='margin:0;font-size:22px;line-height:1.3;color:#FFFFFF;'>"
        f"{escaped_title}</h1>"
        "</td></tr>"
        "<tr><td style='padding:24px 28px;'>"
        "<div style='background-color:#EFF6FF;border-left:4px solid #2563EB;"
        "border-radius:0 8px 8px 0;padding:14px 16px;margin-bottom:20px;'>"
        "<div style='font-size:11px;font-weight:700;text-transform:uppercase;"
        "letter-spacing:0.08em;color:#1D4ED8;margin-bottom:6px;'>Executive Summary</div>"
        f"<p style='margin:0;font-size:14px;line-height:1.6;color:#1E293B;'>"
        f"{escaped_exec}</p></div>"
        f"{''.join(topic_sections)}"
        f"{trends_html}"
        "<p style='margin:28px 0 0 0;padding-top:14px;border-top:1px solid #E2E8F0;"
        "font-size:12px;color:#94A3B8;text-align:center;'>"
        "Generated by Agentic Emailer with deterministic citation grounding verification."
        "</p>"
        "</td></tr></table></td></tr></table></body></html>"
    )


def build_fallback_draft(
    articles: list[RawArticle],
    topics: list[str],
    *,
    max_highlights: int = 8,
) -> DigestDraft:
    """Build a guaranteed-grounded deterministic fallback digest without any LLM calls."""
    clean_topics = [t.strip() for t in topics if t and t.strip()]
    if clean_topics:
        joined = " & ".join(clean_topics[:3])
        suffix = " & More" if len(clean_topics) > 3 else ""
        title = f"{joined}{suffix} Digest"
    else:
        title = "Daily News Digest"

    if not articles:
        return DigestDraft(
            title=title,
            executive_summary=(
                "No newly published articles matched your configured topics and feeds "
                "within the current lookback window."
            ),
            highlights=[],
            notable_trends=[],
        )

    default_topic = clean_topics[0] if clean_topics else "General"
    highlights: list[DigestHighlight] = []
    seen_canonical: set[str] = set()

    for article in articles:
        if len(highlights) >= max_highlights:
            break
        if article.canonical_url in seen_canonical:
            continue
        seen_canonical.add(article.canonical_url)

        topic = next(
            (hint.strip() for hint in article.topic_hints if hint and hint.strip()),
            default_topic,
        )
        summary = article.snippet.strip() or f"Published by {article.source}."
        why = f"Direct update from {article.source} covering {topic}."
        highlights.append(
            DigestHighlight(
                title=article.title,
                topic=topic,
                summary=summary,
                why_it_matters=why,
                story_status="new",
                citations=[
                    Citation(
                        source_name=article.source,
                        url=article.url,
                        article_id=article.article_id,
                    )
                ],
            )
        )

    return DigestDraft(
        title=f"{title} (Direct Source Briefing)",
        executive_summary=(
            f"Compiled {len(highlights)} verified source update(s) directly from your "
            f"configured feeds and search results while automated editorial synthesis was skipped."
        ),
        highlights=highlights,
        notable_trends=[],
    )
