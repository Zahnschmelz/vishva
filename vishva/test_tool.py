#!/usr/bin/env python3
"""
test_tool.py — manueller Tool-Tester für Vishva.
Ruft Tools exakt so auf wie der Agent (ToolManager.execute_tool).

Usage:
  python -m vishva.test_tool list
  python -m vishva.test_tool info <tool>
  python -m vishva.test_tool call <tool> '<json-args>'
"""
import sys
import json
import argparse

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.syntax import Syntax

from .paths import p
from .tool_manager import ToolManager

console = Console()


def load_config() -> dict:
    try:
        with open(p("config", "config.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        console.print(f"[red]⚠️ Config konnte nicht geladen werden: {e}[/red]")
        return {}


def cmd_list(tm: ToolManager) -> int:
    table = Table(title="Verfügbare Tools")
    table.add_column("Name", style="bold cyan", no_wrap=True)
    table.add_column("Aktiv", justify="center")
    table.add_column("Handler", style="dim")
    table.add_column("Description")
    for tool in tm.available_tools:
        func = tool.get("function", {})
        name = func.get("name", "")
        active = "🟢" if name in tm.active_tools else "🔴"
        handler = tm._HANDLERS.get(name, "❌ fehlt")
        table.add_row(name, active, handler, func.get("description", "")[:90])
    console.print(table)
    console.print(
        f"\n[dim]{len(tm.available_tools)} Tools im Pool | "
        f"{len(tm.active_tools)} aktiv[/dim]")

    drift = tm.validate_registry()
    if drift["pool_ohne_handler"]:
        console.print(f"[red]⚠️ Pool-Tools ohne Handler: {drift['pool_ohne_handler']}[/red]")
    if drift["handler_ohne_pool"]:
        console.print(f"[yellow]⚠️ Handler ohne Pool-Eintrag: {drift['handler_ohne_pool']}[/yellow]")
    return 0


def cmd_info(tm: ToolManager, name: str) -> int:
    tool = next(
        (t for t in tm.available_tools
         if t.get("function", {}).get("name") == name), None)
    if not tool:
        console.print(f"[red]Tool '{name}' nicht im Pool gefunden.[/red]")
        names = sorted(t.get("function", {}).get("name", "")
                       for t in tm.available_tools)
        console.print(f"[dim]Verfügbar: {', '.join(names)}[/dim]")
        return 1

    func = tool.get("function", {})
    params = func.get("parameters", {})
    props = params.get("properties", {})
    required = params.get("required", [])

    state = "🟢 aktiv" if name in tm.active_tools else "🔴 inaktiv"
    handler = tm._HANDLERS.get(name, "❌ kein Handler registriert")
    console.print(Panel(
        f"[bold]{func.get('description', '(keine Beschreibung)')}[/bold]\n\n"
        f"[dim]Status: {state} | Handler: {handler}[/dim]",
        title=f"🔧 {name}", border_style="cyan"))

    if props:
        table = Table(title="Parameter")
        table.add_column("Name", style="bold")
        table.add_column("Typ", style="cyan")
        table.add_column("Pflicht", justify="center")
        table.add_column("Details")
        for pname, pschema in props.items():
            ptype = pschema.get("type", "?")
            req = "✅" if pname in required else ""
            details = []
            if "enum" in pschema:
                details.append("enum: " + " | ".join(str(x) for x in pschema["enum"]))
            if "minimum" in pschema:
                details.append(f"min={pschema['minimum']}")
            if "maximum" in pschema:
                details.append(f"max={pschema['maximum']}")
            if pschema.get("description"):
                details.append(pschema["description"])
            table.add_row(pname, ptype, req, "; ".join(details))
        console.print(table)
    else:
        console.print("[dim]Keine Parameter definiert.[/dim]")

    # Beispiel-Aufruf generieren
    example = {}
    for pname, pschema in props.items():
        if pname in required:
            example[pname] = f"<{pschema.get('type', 'string')}>"
    console.print(
        f"\n[bold]Beispiel-Aufruf:[/bold]\n"
        f"[green]python -m vishva.test_tool call {name} "
        f"'{json.dumps(example, ensure_ascii=False)}'[/green]")
    return 0


def cmd_call(tm: ToolManager, name: str, args_json: str) -> int:
    pooled_names = {t.get("function", {}).get("name", "")
                    for t in tm.available_tools}
    if name not in pooled_names and name not in tm._HANDLERS:
        console.print(f"[red]Tool '{name}' unbekannt.[/red]")
        console.print(f"[dim]Verfügbar: {', '.join(sorted(pooled_names))}[/dim]")
        return 1
    if name not in tm.active_tools:
        console.print(
            "[yellow]⚠️ Tool ist nicht aktiv (activetools.txt) — "
            "wird trotzdem ausgeführt.[/yellow]")

    try:
        args = json.loads(args_json) if args_json.strip() else {}
    except json.JSONDecodeError as e:
        console.print(f"[red]Ungültiges JSON: {e}[/red]")
        return 1
    if not isinstance(args, dict):
        console.print("[red]Args müssen ein JSON-Objekt sein.[/red]")
        return 1

    console.print(
        f"[bold cyan]▶ {name}[/bold cyan] "
        f"[dim]{json.dumps(args, ensure_ascii=False)[:300]}[/dim]\n")

    result = tm.execute_tool(name, args)

    is_error = isinstance(result, dict) and "error" in result
    out = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    console.print(Panel(
        Syntax(out, "json", word_wrap=True, background_color="default"),
        title=f"{'❌ Fehler' if is_error else '✅ Ergebnis'}: {name}",
        border_style="red" if is_error else "green"))
    return 1 if is_error else 0


def main():
    parser = argparse.ArgumentParser(
        description="Manueller Tool-Tester für Vishva")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="Alle Tools auflisten")

    p_info = sub.add_parser("info", help="Beschreibung + Parameter anzeigen")
    p_info.add_argument("tool")

    p_call = sub.add_parser("call", help="Tool aufrufen (wie der Agent)")
    p_call.add_argument("tool")
    p_call.add_argument(
        "args", nargs="?", default="{}",
        help='Args als JSON, z.B. \'{"url": "https://example.com"}\'')

    parsed = parser.parse_args()
    if not parsed.cmd:
        parser.print_help()
        return 0

    config = load_config()
    tm = ToolManager(config=config)

    if parsed.cmd == "list":
        return cmd_list(tm)
    if parsed.cmd == "info":
        return cmd_info(tm, parsed.tool)
    if parsed.cmd == "call":
        return cmd_call(tm, parsed.tool, parsed.args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
