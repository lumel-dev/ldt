<#
.SYNOPSIS
  Deja `ldt` listo para usar: en el PATH, y la regla para los agentes en su archivo de
  instrucciones global.

.DESCRIPTION
  Es idempotente: se puede correr las veces que haga falta. La regla se escribe entre
  marcadores, asi que una segunda corrida la reemplaza en vez de duplicarla.

  Por defecto escribe en `~/.claude/CLAUDE.md`. Con -AgentFile se puede apuntar a otro
  archivo (p. ej. `~/AGENTS.md`), y con -NoRule se saltea ese paso.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -AgentFile "$HOME\AGENTS.md"
#>
param(
    [string]$AgentFile,
    [switch]$NoRule
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path

# --- 1. `ldt` en el PATH del usuario -----------------------------------------------
$bin = Join-Path $repo 'bin'
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -notlike "*$bin*") {
    [Environment]::SetEnvironmentVariable('Path', "$userPath;$bin", 'User')
    Write-Host "PATH: agregado $bin (abrir una terminal nueva para que tome efecto)"
} else {
    Write-Host "PATH: $bin ya estaba"
}

# --- 2. regla para los agentes ------------------------------------------------------
# Es lo que hace que un agente sepa que `ldt` existe sin que haya que contarselo en cada
# sesion. Si preferis pegarla a mano, el texto esta en rules/ldt.md.
if (-not $NoRule) {
    if (-not $AgentFile) { $AgentFile = Join-Path $env:USERPROFILE '.claude\CLAUDE.md' }

    $rulePath = Join-Path $repo 'rules\ldt.md'
    $rule = (Get-Content $rulePath -Raw -Encoding UTF8) -replace '<RUTA-DEL-REPO>', ($repo -replace '\\', '/')
    $start = '<!-- ldt:start -->'
    $end = '<!-- ldt:end -->'
    $block = "$start`n$rule`n$end"

    $dir = Split-Path -Parent $AgentFile
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    if (Test-Path $AgentFile) {
        $md = Get-Content $AgentFile -Raw -Encoding UTF8
    } else {
        $md = ''
    }
    if ($md -match [regex]::Escape($start)) {
        $pattern = [regex]::Escape($start) + '[\s\S]*?' + [regex]::Escape($end)
        $md = [regex]::Replace($md, $pattern, [System.Text.RegularExpressions.MatchEvaluator] { param($m) $block })
    } else {
        if ($md.Trim().Length -gt 0) { $md = $md.TrimEnd() + "`n`n" }
        $md = $md + $block + "`n"
    }
    # WriteAllText con UTF8Encoding($false): Set-Content -Encoding UTF8 mete BOM en PS 5.1.
    [System.IO.File]::WriteAllText($AgentFile, $md, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "regla: escrita en $AgentFile"
}

# --- 3. dependencias ----------------------------------------------------------------
Write-Host ''
python (Join-Path $repo 'ldt.py') doctor
