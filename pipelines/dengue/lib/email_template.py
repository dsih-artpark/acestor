"""Government-facing HTML email template for the dengue intelligence report.

Designed to be read by health officials at the state/district level.
All content is non-technical — no references to pipeline internals.

Config keys (all under ``report_distribution`` in the pipeline YAML):

    report_distribution:
      system_name: "Dengue Early Warning System"   # shown in header
      organization: "ARTPARK, IISc Bengaluru"       # shown in footer
      state: "Karnataka"                            # e.g. "Andhra Pradesh", "Odisha"
      region: "Greater Bengaluru Area (GBA)"        # geographic scope label
      department: "Directorate of Health & Family Welfare, GoK"
      contact_email: ""                             # optional reply-to contact
      footer_note: ""                               # optional extra disclaimer line
"""

from __future__ import annotations

from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Brand / palette — tweak here if state branding changes
# ---------------------------------------------------------------------------

_HEADER_BG = "#0d3b6e"  # deep navy
_ACCENT = "#1a7abf"  # ARTPARK blue
_STRIPE_DARK = "#f0f4f8"
_TEXT_MAIN = "#1a1a2e"
_TEXT_MUTED = "#6b7280"
_GREEN_BG = "#e6f4ea"
_GREEN_FG = "#1e7e34"


def _fix_date(value: str) -> str:
    """Strip LaTeX double-dash encoding (2026--03--18 → 2026-03-18)."""
    return str(value).replace("--", "-")


def _week_rows(listdates: list[str]) -> str:
    if not listdates:
        return "<tr><td colspan='2' style='padding:8px 0;color:#999;font-size:13px'>No prediction weeks available</td></tr>"
    rows = ""
    for i, d in enumerate(listdates):
        bg = _STRIPE_DARK if i % 2 == 0 else "#ffffff"
        rows += (
            f"<tr style='background:{bg}'>"
            f"<td style='padding:8px 12px;font-size:13px;color:{_TEXT_MAIN}'>Week {i+1}</td>"
            f"<td style='padding:8px 12px;font-size:13px;color:{_TEXT_MAIN};font-weight:600'>{_fix_date(d)}</td>"
            f"</tr>"
        )
    return rows


def build_govt_email_html(
    *,
    rep_dict: dict,
    cfg: dict,
    pdf_filename: str | None = None,
    maps_filename: str | None = None,
) -> str:
    """Return the HTML body for the government-facing report email.

    Args:
        rep_dict:       Parsed ``rep_dict`` JSON from the report step.
        cfg:            Full pipeline config dict (``context.config``).
        pdf_filename:   Basename of the attached PDF report, or None.
        maps_filename:  Basename of the attached maps zip, or None.
    """
    dist: dict = cfg.get("report_distribution") or {}

    system_name = dist.get("system_name") or "Dengue Early Warning System"
    organization = dist.get("organization") or "ARTPARK, IISc Bengaluru"
    state = dist.get("state") or ""
    region = dist.get("region") or (cfg.get("pipeline") or {}).get("title") or "Region"
    department = dist.get("department") or ""
    contact_email = dist.get("contact_email") or ""
    footer_note = dist.get("footer_note") or ""

    reportmonth = rep_dict.get("reportmonth") or "—"
    prediction_date = _fix_date(rep_dict.get("prediction_date") or "—")
    epi_start = _fix_date(rep_dict.get("epi_data_start_date") or "—")
    epi_end = _fix_date(rep_dict.get("epi_data_end_date") or "—")
    weather_end = _fix_date(rep_dict.get("weather_data_end_date") or "—")

    listdates_zone = rep_dict.get("listdates_zone") or []
    listdates_corp = rep_dict.get("listdates_corp") or []
    n_zone_weeks = len(listdates_zone)

    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    # ---- Attachments section -----------------------------------------------
    attachment_rows = ""
    if pdf_filename:
        attachment_rows += f"""
        <tr>
          <td style="padding:10px 16px;border-bottom:1px solid #e5e7eb;font-size:13px">
            <span style="font-size:16px;margin-right:8px">📄</span>
            <strong>Detailed Risk Assessment Report</strong><br>
            <span style="color:{_TEXT_MUTED};font-size:12px">{pdf_filename}</span>
          </td>
        </tr>"""
    if maps_filename:
        attachment_rows += f"""
        <tr>
          <td style="padding:10px 16px;border-bottom:1px solid #e5e7eb;font-size:13px">
            <span style="font-size:16px;margin-right:8px">🗺</span>
            <strong>District / Zone-wise Risk Maps</strong><br>
            <span style="color:{_TEXT_MUTED};font-size:12px">{maps_filename}</span>
          </td>
        </tr>"""
    if not attachment_rows:
        attachment_rows = f"""
        <tr>
          <td style="padding:10px 16px;font-size:13px;color:{_TEXT_MUTED}">
            No attachments for this run.
          </td>
        </tr>"""

    # ---- Prediction weeks --------------------------------------------------
    zone_week_rows = _week_rows(listdates_zone)
    corp_week_rows = _week_rows(listdates_corp) if listdates_corp else ""

    corp_section = ""
    if listdates_corp:
        corp_section = f"""
      <div style="margin-top:24px">
        <div style="font-size:11px;font-weight:700;color:{_TEXT_MUTED};text-transform:uppercase;letter-spacing:0.8px;margin-bottom:8px">
          Corporation-level Prediction Weeks
        </div>
        <table style="width:100%;border-collapse:collapse;border-radius:6px;overflow:hidden;border:1px solid #e5e7eb">
          {corp_week_rows}
        </table>
      </div>"""

    # ---- State badge -------------------------------------------------------
    state_badge = (
        f"<span style='background:rgba(255,255,255,0.15);color:#fff;"
        f"font-size:11px;padding:3px 10px;border-radius:12px;"
        f"margin-left:10px;vertical-align:middle'>{state}</span>"
        if state
        else ""
    )

    department_line = (
        f"<div style='font-size:12px;color:#90b8d8;margin-top:4px'>{department}</div>"
        if department
        else ""
    )

    contact_line = (
        f"<a href='mailto:{contact_email}' style='color:{_ACCENT};font-size:12px'>{contact_email}</a>"
        if contact_email
        else ""
    )

    extra_footer = (
        f"<div style='margin-top:8px;font-size:11px;color:{_TEXT_MUTED}'>{footer_note}</div>"
        if footer_note
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#eef2f7;font-family:Arial,Helvetica,sans-serif">

<div style="max-width:680px;margin:28px auto 40px;background:#ffffff;border-radius:10px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.10)">

  <!-- ── Header ─────────────────────────────────────────────────────────── -->
  <div style="background:{_HEADER_BG};padding:28px 32px 22px">
    <div style="display:flex;align-items:center;justify-content:space-between">
      <div>
        <div style="font-size:11px;color:#90b8d8;text-transform:uppercase;letter-spacing:1.2px;margin-bottom:6px">
          {system_name}{state_badge}
        </div>
        <div style="font-size:22px;font-weight:700;color:#ffffff;line-height:1.3">
          Dengue Risk Intelligence Report
        </div>
        <div style="font-size:15px;color:#ccdff0;margin-top:4px">{region} &nbsp;·&nbsp; {reportmonth}</div>
        {department_line}
      </div>
      <div style="text-align:right">
        <div style="background:{_ACCENT};color:#fff;font-size:11px;font-weight:700;padding:4px 12px;border-radius:20px;letter-spacing:0.5px">
          SYSTEM GENERATED
        </div>
        <div style="color:#90b8d8;font-size:11px;margin-top:8px">{generated_at}</div>
      </div>
    </div>
  </div>

  <!-- ── Summary strip ──────────────────────────────────────────────────── -->
  <div style="background:{_ACCENT};padding:14px 32px;display:flex;gap:32px">
    <div style="color:#fff">
      <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.8px;opacity:0.8">Report Period</div>
      <div style="font-size:15px;font-weight:700;margin-top:2px">{reportmonth}</div>
    </div>
    <div style="color:#fff;border-left:1px solid rgba(255,255,255,0.3);padding-left:32px">
      <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.8px;opacity:0.8">Computed On</div>
      <div style="font-size:15px;font-weight:700;margin-top:2px">{prediction_date}</div>
    </div>
    <div style="color:#fff;border-left:1px solid rgba(255,255,255,0.3);padding-left:32px">
      <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.8px;opacity:0.8">Prediction Weeks</div>
      <div style="font-size:15px;font-weight:700;margin-top:2px">{n_zone_weeks} zone{'' if n_zone_weeks == 1 else 's'}</div>
    </div>
  </div>

  <!-- ── Body ───────────────────────────────────────────────────────────── -->
  <div style="padding:28px 32px">

    <!-- Data coverage -->
    <div style="margin-bottom:28px">
      <div style="font-size:13px;font-weight:700;color:{_TEXT_MUTED};text-transform:uppercase;letter-spacing:0.8px;border-bottom:2px solid {_ACCENT};padding-bottom:6px;margin-bottom:14px">
        Data Coverage
      </div>
      <table style="width:100%;border-collapse:collapse">
        <tr style="background:{_STRIPE_DARK}">
          <td style="padding:10px 14px;font-size:13px;color:{_TEXT_MUTED};width:45%">Disease Surveillance Period</td>
          <td style="padding:10px 14px;font-size:13px;color:{_TEXT_MAIN};font-weight:600">{epi_start} &nbsp;→&nbsp; {epi_end}</td>
        </tr>
        <tr>
          <td style="padding:10px 14px;font-size:13px;color:{_TEXT_MUTED}">Meteorological Data Through</td>
          <td style="padding:10px 14px;font-size:13px;color:{_TEXT_MAIN};font-weight:600">{weather_end}</td>
        </tr>
        <tr style="background:{_STRIPE_DARK}">
          <td style="padding:10px 14px;font-size:13px;color:{_TEXT_MUTED}">Risk Computed On</td>
          <td style="padding:10px 14px;font-size:13px;color:{_TEXT_MAIN};font-weight:600">{prediction_date}</td>
        </tr>
      </table>
    </div>

    <!-- Prediction weeks -->
    <div style="margin-bottom:28px">
      <div style="font-size:13px;font-weight:700;color:{_TEXT_MUTED};text-transform:uppercase;letter-spacing:0.8px;border-bottom:2px solid {_ACCENT};padding-bottom:6px;margin-bottom:14px">
        Prediction Weeks Covered
      </div>
      <table style="width:100%;border-collapse:collapse;border:1px solid #e5e7eb;border-radius:6px;overflow:hidden">
        {zone_week_rows}
      </table>
      {corp_section}
    </div>

    <!-- Attachments -->
    <div style="margin-bottom:8px">
      <div style="font-size:13px;font-weight:700;color:{_TEXT_MUTED};text-transform:uppercase;letter-spacing:0.8px;border-bottom:2px solid {_ACCENT};padding-bottom:6px;margin-bottom:14px">
        Attachments
      </div>
      <table style="width:100%;border-collapse:collapse;border:1px solid #e5e7eb;border-radius:6px;overflow:hidden">
        {attachment_rows}
      </table>
    </div>

  </div>

  <!-- ── Footer ─────────────────────────────────────────────────────────── -->
  <div style="background:#f8fafc;border-top:1px solid #e5e7eb;padding:16px 32px">
    <div style="font-size:12px;color:{_TEXT_MUTED}">
      This is an automated report generated by the <strong>{system_name}</strong> &mdash; {organization}.
      {contact_line}
    </div>
    {extra_footer}
  </div>

</div>

</body>
</html>"""
