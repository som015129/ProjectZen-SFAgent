"""SAP SuccessFactors agentic data assistant.

LangGraph ReAct agent driven by Claude Sonnet 4.6, with SF expertise baked into
the system prompt and tools that wrap the OData client.

Run interactively:
    python sf_agent.py

Or import and call run() programmatically:
    from sf_agent import run
    df = run("Pull all employees with name, DOB, hire date in current month")
"""
import os
import re
import sys
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import pandas as pd
from lxml import etree
from rich.console import Console
from rich.table import Table
from dotenv import load_dotenv

import warnings
from langchain_core.tools import tool
from langchain_anthropic import ChatAnthropic
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from langgraph.prebuilt import create_react_agent

from sf_client import SuccessFactorsClient

load_dotenv()

_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not _ANTHROPIC_KEY:
    raise EnvironmentError(
        "ANTHROPIC_API_KEY is missing or commented out in .env. "
        "Add: ANTHROPIC_API_KEY = \"sk-ant-...\""
    )

console = Console()
_client = SuccessFactorsClient()

# Captured by query_odata so the reporter can pick up the last result.
_last: dict = {"rows": [], "entity": None, "params": None}

_metadata_root = None  # cached parsed $metadata XML
_EDM_NS = {
    "edmx": "http://schemas.microsoft.com/ado/2007/06/edmx",
    "edm": "http://schemas.microsoft.com/ado/2008/09/edm",
}


def _fetch_metadata():
    global _metadata_root
    if _metadata_root is not None:
        return _metadata_root
    url = f"{_client.base_url}/odata/v2/$metadata"
    headers = {"Authorization": f"Bearer {_client._get_token()}",
               "Accept": "application/xml"}
    resp = _client.session.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    _metadata_root = etree.fromstring(resp.content)
    return _metadata_root


# ---------- Tools ----------

@tool
def current_date_context() -> str:
    """Today's date plus useful relative date ranges for building OData $filter expressions.
    Call this whenever the user uses relative dates ("current month", "last 30 days", "this year")."""
    today = datetime.now(timezone.utc).date()
    month_start = today.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    return json.dumps({
        "today": today.isoformat(),
        "current_month_start": month_start.isoformat(),
        "current_month_end_exclusive": next_month.isoformat(),
        "current_year_start": today.replace(month=1, day=1).isoformat(),
        "last_30_days_start": (today - timedelta(days=30)).isoformat(),
        "odata_datetime_literal": "datetime'YYYY-MM-DDTHH:MM:SS' (e.g. datetime'2026-05-01T00:00:00')",
    })


@tool
def describe_entity(entity: str) -> str:
    """Return the schema for an SF OData entity: properties (with types/labels)
    and navigation properties. Use this when uncertain about field names."""
    root = _fetch_metadata()
    et = root.find(f".//edm:EntityType[@Name='{entity}']", _EDM_NS)
    if et is None:
        # try case-insensitive match
        for cand in root.iterfind(".//edm:EntityType", _EDM_NS):
            if cand.get("Name", "").lower() == entity.lower():
                et = cand
                break
    if et is None:
        return json.dumps({"error": f"Entity '{entity}' not found.",
                           "hint": "Check spelling. Common entities: User, PerPerson, PerPersonal, EmpEmployment, EmpJob, EmpCompensation."})

    label_attr = "{http://www.successfactors.com/edm/sf}label"
    props = []
    for p in et.findall("edm:Property", _EDM_NS):
        props.append({
            "name": p.get("Name"),
            "type": p.get("Type", "").replace("Edm.", ""),
            "nullable": p.get("Nullable", "true"),
            "label": p.get(label_attr, ""),
        })
    navs = []
    for n in et.findall("edm:NavigationProperty", _EDM_NS):
        navs.append({"name": n.get("Name"), "relationship": n.get("Relationship", "")})

    out = {"entity": et.get("Name"),
           "property_count": len(props),
           "properties": props[:80],  # cap to keep tokens reasonable
           "navigation_properties": navs}
    return json.dumps(out, indent=2)[:8000]


def _flatten_row(obj: dict, prefix: str = "", out: dict = None) -> dict:
    """Recursively flatten an SF OData row, including nested nav properties.

    SF returns nav expansions as {"results": [{...}]} dicts. This picks the
    first element and flattens its fields, stripping __metadata / __deferred.
    Column names use the leaf field name only (last segment), deduped by prefix.
    """
    if out is None:
        out = {}
    if not isinstance(obj, dict):
        return out
    for k, v in obj.items():
        if k in ("__metadata", "__deferred"):
            continue
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            if "results" in v:
                results = v.get("results") or []
                if results and isinstance(results[0], dict):
                    _flatten_row(results[0], key, out)
            elif "__deferred" in v:
                continue
            else:
                _flatten_row(v, key, out)
        elif isinstance(v, list):
            if v and isinstance(v[0], dict):
                _flatten_row(v[0], key, out)
        else:
            out[key] = v
    return out


def _shorten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename dotted nav-path columns to their leaf name, deduping if needed."""
    seen: dict[str, int] = {}
    rename = {}
    for col in df.columns:
        leaf = col.split(".")[-1]
        if leaf in seen:
            seen[leaf] += 1
            rename[col] = f"{leaf}_{seen[leaf]}"
        else:
            seen[leaf] = 0
            rename[col] = leaf
    return df.rename(columns=rename)


@tool
def query_odata(entity: str, select: str = "", filter: str = "",
                expand: str = "", top: int = 100,
                from_date: str = "", to_date: str = "") -> str:
    """Execute an SF OData v2 query against `entity`. Args:
    - entity: e.g. 'User', 'PerPerson', 'EmpEmployment'
    - select: comma-separated $select fields
    - filter: $filter expression in OData v2 syntax
    - expand: comma-separated $expand navigation properties
    - top: max rows (default 100)
    - from_date / to_date: YYYY-MM-DD — controls WHICH EFFECTIVE-DATED SNAPSHOT is returned,
      NOT a creation-date filter. For Foundation Objects (FOCostCenter, FODepartment, etc.)
      and EmpJob/EmpCompensation/PerPersonal: omit (defaults to today) to get the CURRENT
      active version. To search by creation date, use $filter on `createdOn` instead.

    Returns a summary with row count, columns, and a 3-row sample. Full result
    is stashed for the reporter to pick up afterwards.
    """
    params = {"$format": "json"}
    if select:
        params["$select"] = select
    if filter:
        params["$filter"] = filter
    if expand:
        params["$expand"] = expand
    if top:
        params["$top"] = top
    if from_date:
        params["fromDate"] = from_date
    if to_date:
        params["toDate"] = to_date

    try:
        result = _client.get_entity(entity, params)
    except Exception as e:
        return json.dumps({"error": str(e), "params": params})

    rows = result.get("d", {}).get("results", [])
    cleaned_rows = [_flatten_row(r) for r in rows]
    df = pd.DataFrame(cleaned_rows)

    _last["rows"] = cleaned_rows
    _last["entity"] = entity
    _last["params"] = params

    summary = {
        "entity": entity,
        "rows_returned": len(cleaned_rows),
        "columns": list(df.columns),
        "params": params,
        "sample_rows": cleaned_rows[:3],
    }
    return json.dumps(summary, default=str)[:6000]


TOOLS = [current_date_context, describe_entity, query_odata]


# ---------- System prompt ----------

SF_SYSTEM_PROMPT = """You are an expert SAP SuccessFactors data analyst with deep knowledge of all SF modules.

# Modules & key entities

## Employee Central (EC) — most common
- **User**: userId, firstName, lastName, email, status, manager, department, division, jobTitle, hireDate (for legacy compat)
- **PerPerson**: root person entity (personIdExternal); use $expand=personalInfoNav,employmentNav,phoneNav,emailNav,nationalIdNav,addressNav
- **PerPersonal** (effective-dated): firstName, lastName, dateOfBirth, gender, marriedStatus
- **PerEmail / PerPhone / PerAddressDEFLT**
- **EmpEmployment** (NOT effective-dated): userId, personIdExternal, startDate, endDate, originalStartDate, hireDate, terminationDate, isContingentWorker
- **EmpJob** (effective-dated): userId, jobCode, departmentNav, locationNav, companyNav, managerEmployee, employmentTypeNav, startDate, endDate
- **EmpCompensation** (effective-dated): payComponentNonRecurring, frequency, currency
- **FOCostCenter / FOCompany / FOLocation / FODepartment / FODivision / FOBusinessUnit** (Foundation Objects, effective-dated — see rules below)
- **Position** (effective-dated): code, positionTitle, effectiveStatus, toBeFilled, maxNumOfEmployees, incumbentId, department, location, jobCode, startDate, endDate, createdOn

## Other modules
- **Onboarding**: OnboardingTask, OnbProcess, OnbCandidate
- **Recruiting (RCM)**: JobRequisition, JobApplication, Candidate
- **Performance & Goals**: FormHeader, Goal, GoalPlanTemplate
- **Succession**: SuccessorIncumbents, SuccessionPlan, TalentPool
- **Learning (LMS)**: LearningHistory, LearningPlan, Curriculum
- **Compensation Planning**: CompensationForm, CompensationFormItem

# OData v2 syntax (SF)

- `$select`: `userId,firstName,lastName`
- `$filter` operators: `eq ne gt ge lt le and or`
- Strings: single-quoted `firstName eq 'John'`
- Dates: `startDate ge datetime'2026-05-01T00:00:00'`
- `$expand`: `personalInfoNav,employmentNav`
- For **effective-dated entities** (EmpJob, EmpCompensation, PerPersonal, FO*):
  - `fromDate` / `toDate` control WHICH VERSION snapshot is returned, not a filter
  - Omit `fromDate` (or use today) to get the **currently active** version
  - Use `$filter` on `createdOn` to find records **created** in a date range

# Position entity — CRITICAL rules

The `Position` entity tracks headcount slots. Key fields:

| Field | Type | Meaning |
|---|---|---|
| `code` | String | Position number / ID |
| `positionTitle` | String | Job title of the position |
| `effectiveStatus` | String | `'A'` = Active, `'I'` = Inactive |
| `toBeFilled` | String | **`'Yes'`** = VACANT (needs to be hired), `'No'` = filled / not open |
| `maxNumOfEmployees` | Int | Approved headcount slots |
| `incumbentId` | String | userId of current holder (may be null even for non-vacant positions) |
| `startDate` | Date | Effective start of this version |

**Vacancy rule (MANDATORY):**
- ALWAYS filter `toBeFilled eq 'Yes'` to find vacant positions.
- Do **NOT** use `incumbent eq null` — that OData expression is invalid in SF and will return an error.
- `incumbentId` being null alone does NOT mean a position is vacant; only `toBeFilled eq 'Yes'` is authoritative.
- Combine with `effectiveStatus eq 'A'` to exclude inactive positions.

**Correct vacant position query:**
```
entity=Position, filter=effectiveStatus eq 'A' and toBeFilled eq 'Yes', select=code,positionTitle,toBeFilled,maxNumOfEmployees,incumbentId
```

# Foundation Object (FO*) rules — CRITICAL

Foundation Objects (FOCostCenter, FODepartment, FOLocation, etc.) are master-data records
with effective dating. There are two distinct date concepts:

| Field | Meaning |
|---|---|
| `startDate` | Effective start date of THIS version of the record |
| `createdOn` | When the record was first entered into the system |
| `fromDate` param | Which effective-dated snapshot to return (NOT a creation filter) |

**Rules:**
1. To get the **current active version** of an FO record → omit `from_date` (SF defaults to today).
2. To find FO records **created in a date range** → use `$filter=createdOn ge datetime'YYYY-MM-DDT00:00:00' and createdOn lt datetime'YYYY-MM-DDT00:00:00'` — do NOT use `from_date` for this.
3. **Never** set `from_date` to the start of a month when looking for "records created this month" — you will get a stale snapshot from that past date, not newly created records.
4. The `startDate` returned for an FO is its effective-dated version start, which may differ from `createdOn`.

# How to work

1. Parse what the user wants — entity, fields, filters.
2. If relative dates ("current month", "last week", "this quarter"), call `current_date_context` first.
3. If unsure where a field lives, call `describe_entity` for the candidate entity.
4. Compose ONE `query_odata` call — prefer $expand over multiple round-trips.
   - For FO entities: omit `from_date` to get current records; use `$filter` on `createdOn` to filter by creation date.
5. Reply with a one-sentence summary: what was fetched, how many rows. The reporter prints the table — do NOT re-list rows in your reply.

# Common mappings

- "name" → User.firstName + User.lastName (or PerPersonal via PerPerson.personalInfoNav)
- "date of birth" / "DOB" → PerPersonal.dateOfBirth (reach via PerPerson + $expand=personalInfoNav)
- "hire date" → EmpEmployment.startDate (or originalStartDate for very first hire)
- "termination date" → EmpEmployment.endDate or terminationDate
- "manager" → EmpJob.managerEmployee (effective-dated)
- "department/location/company" → EmpJob.departmentNav/locationNav/companyNav with $expand
- "cost center created in May" → FOCostCenter with $filter=createdOn ge datetime'2026-05-01T00:00:00' (NOT fromDate)
- "effective start date of cost center" → FOCostCenter.startDate (returned when querying without fromDate = current version)
- "vacant positions" / "open positions" → Position with $filter=effectiveStatus eq 'A' and toBeFilled eq 'Yes' — NEVER use incumbent eq null
- "position number" → Position.code
- "how many people can fill a position" → Position.maxNumOfEmployees
"""


# ---------- Agent ----------

def build_agent():
    llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0, max_tokens=4096,
                        api_key=_ANTHROPIC_KEY)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return create_react_agent(llm, TOOLS, prompt=SF_SYSTEM_PROMPT)


# ---------- Reporter ----------

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower())[:50].strip("_") or "report"


_ODATA_DATE_RE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _convert_odata_date(v):
    """Convert OData v2 '/Date(epoch_ms)/' strings to ISO date strings.

    Uses timedelta arithmetic instead of fromtimestamp() to avoid the Windows
    OSError [Errno 22] raised for far-future dates like 9999-12-31.
    """
    if not isinstance(v, str):
        return v
    m = _ODATA_DATE_RE.match(v)
    if not m:
        return v
    try:
        epoch_ms = int(m.group(1))
        dt = _EPOCH + timedelta(milliseconds=epoch_ms)
        return dt.date().isoformat() if dt.hour == dt.minute == dt.second == 0 else dt.isoformat()
    except (OverflowError, ValueError):
        return v


def to_dataframe() -> pd.DataFrame:
    df = pd.DataFrame(_last["rows"])
    if df.empty:
        return df
    df = _shorten_columns(df)
    return df.map(_convert_odata_date)


def print_console_table(df: pd.DataFrame, title: str = "SF Report", max_rows: int = 30):
    if df.empty:
        console.print(f"[yellow]No rows for {title}[/yellow]")
        return
    table = Table(title=title, show_lines=False, header_style="bold magenta")
    for col in df.columns:
        table.add_column(str(col), overflow="fold")
    for _, row in df.head(max_rows).iterrows():
        table.add_row(*["" if pd.isna(v) else str(v) for v in row])
    console.print(table)
    if len(df) > max_rows:
        console.print(f"[dim]({len(df) - max_rows} more rows in CSV/Excel)[/dim]")


_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Strip control characters that openpyxl can't write to XML cells."""
    def _clean(v):
        if isinstance(v, str):
            return _ILLEGAL_XML_RE.sub("", v)
        return v
    return df.map(_clean)


def save_outputs(df: pd.DataFrame, prompt: str) -> tuple[Path, Path]:
    out_dir = Path("reports")
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = out_dir / f"{ts}_{_slug(prompt)}"
    csv_path = base.with_suffix(".csv")
    xlsx_path = base.with_suffix(".xlsx")

    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    try:
        clean_df = _sanitize_df(df)
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            clean_df.to_excel(writer, index=False, sheet_name="report")
            ws = writer.sheets["report"]
            # Auto-fit column widths
            for col in ws.columns:
                max_len = max((len(str(c.value)) for c in col if c.value), default=10)
                ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)
    except Exception as e:
        console.print(f"[yellow]Excel save failed ({e}), CSV still saved.[/yellow]")
        xlsx_path = None

    return csv_path, xlsx_path


# ---------- Public entry point ----------

def run(prompt: str) -> pd.DataFrame:
    _last["rows"], _last["entity"], _last["params"] = [], None, None

    console.rule(f"[bold cyan]Prompt[/bold cyan]")
    console.print(prompt)
    console.rule()

    agent = build_agent()
    result = agent.invoke({"messages": [("user", prompt)]})

    # Print each tool call the agent made (visibility into reasoning)
    for msg in result["messages"]:
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                console.print(f"[dim]→ {tc['name']}({json.dumps(tc['args'])[:200]})[/dim]")

    final = result["messages"][-1].content
    if isinstance(final, list):  # Claude returns content blocks sometimes
        final = "".join(b.get("text", "") for b in final if isinstance(b, dict))
    console.print(f"\n[bold green]Agent:[/bold green] {final}\n")

    df = to_dataframe()
    if df.empty:
        console.print("[yellow]No data returned from SF.[/yellow]")
        return df

    title = f"{_last['entity']} — {len(df)} rows"
    print_console_table(df, title=title)
    csv_path, xlsx_path = save_outputs(df, prompt)
    saved = f"[green]Saved →[/green] {csv_path}"
    if xlsx_path:
        saved += f"  |  {xlsx_path}"
    console.print(saved)
    return df


if __name__ == "__main__":
    console.print("[bold]SF Agent[/bold] — type a question, or 'exit'.\n")
    while True:
        try:
            prompt = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print()
            break
        if not prompt or prompt.lower() in ("exit", "quit", "q"):
            break
        try:
            run(prompt)
        except Exception as e:
            console.print(f"[red]Error:[/red] {type(e).__name__}: {e}")
