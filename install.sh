#!/usr/bin/env sh
# Instalador para Linux y macOS: deja `ldt` en el PATH y la regla para los agentes en su
# archivo de instrucciones global. El equivalente en Windows es install.ps1.
#
# Es idempotente: se puede correr las veces que haga falta. La regla se escribe entre
# marcadores, asi que una segunda corrida la reemplaza en vez de duplicarla.
#
#   ./install.sh                             # PATH + regla en ~/.claude/CLAUDE.md
#   ./install.sh --agent-file ~/AGENTS.md    # la regla en otro archivo
#   ./install.sh --bin-dir ~/bin             # el symlink en otro directorio
#   ./install.sh --no-rule                   # solo el PATH
set -e

agent_file=""
bin_dir="${LDT_BIN_DIR:-}"
no_rule=""

while [ $# -gt 0 ]; do
    case $1 in
        --agent-file) agent_file=${2:?--agent-file necesita una ruta}; shift 2 ;;
        --bin-dir) bin_dir=${2:?--bin-dir necesita una ruta}; shift 2 ;;
        --no-rule) no_rule=1; shift ;;
        # El encabezado de arriba es la ayuda: se imprime hasta el primer renglon que no
        # sea comentario, asi no hay numeros de linea que se desincronicen al editarlo.
        -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
        *) echo "install.sh: opcion desconocida: $1" >&2; exit 2 ;;
    esac
done

# Igual que en bin/ldt: sin `readlink -f`, que en BSD/macOS no existe.
self=$0
while [ -L "$self" ]; do
    link=$(ls -ld -- "$self" | sed 's/^.*-> //')
    case $link in
        /*) self=$link ;;
        *) self=$(dirname -- "$self")/$link ;;
    esac
done
repo=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd)

# `python3` puede ser el stub de la Microsoft Store (existe, no es Python): se prueba.
py=""
for cand in python3 python py; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c '' >/dev/null 2>&1; then
        py=$cand
        break
    fi
done
[ -n "$py" ] || { echo "install.sh: hace falta Python 3.10+ en el PATH" >&2; exit 1; }

# --- 1. `ldt` en el PATH ------------------------------------------------------------
# En POSIX no hay un PATH de usuario que se pueda editar como en Windows: vive en el rc
# de cada shell (bash, zsh, fish, cada uno el suyo). Toquetear el rc del usuario es peor
# que no hacerlo, asi que se linkea en un directorio que ya suele estar en el PATH y, si
# no esta, se imprime la linea para agregarlo a mano.
if [ -z "$bin_dir" ]; then
    for d in "$HOME/.local/bin" "$HOME/bin"; do
        if [ -d "$d" ]; then bin_dir=$d; break; fi
    done
fi
: "${bin_dir:=$HOME/.local/bin}"
mkdir -p "$bin_dir"
chmod +x "$repo/bin/ldt"  # por si el clone perdio el bit de ejecucion

link=$bin_dir/ldt
rm -f "$link"
if ln -s "$repo/bin/ldt" "$link" 2>/dev/null && [ -L "$link" ]; then
    echo "PATH: $link -> $repo/bin/ldt"
else
    # Donde no hay symlinks (FAT/exFAT, un share de red, Git Bash sin privilegios) `ln -s`
    # no falla: copia el archivo. Y la copia resuelve el repo a su propio directorio, asi
    # que `ldt` queda buscando ldt.py al lado del link y no arranca. Un wrapper con la
    # ruta absoluta adentro no depende de que el filesystem sepa linkear.
    printf '#!/usr/bin/env sh
exec "%s/bin/ldt" "$@"
' "$repo" > "$link"
    chmod +x "$link"
    echo "PATH: $link (wrapper: este filesystem no soporta symlinks)"
fi
# Probado, no supuesto: un link que no corre es una instalacion que no sirvio.
"$link" -V >/dev/null 2>&1 || { echo "install.sh: $link quedo instalado pero no corre" >&2; exit 1; }
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "      $bin_dir no esta en el PATH: agregar a tu rc  ->  export PATH=\"$bin_dir:\$PATH\"" ;;
esac

# --- 2. regla para los agentes ------------------------------------------------------
# Es lo que hace que un agente sepa que `ldt` existe sin que haya que contarselo en cada
# sesion. Si preferis pegarla a mano, el texto esta en rules/ldt.md.
#
# La cirugia entre marcadores va en Python en vez de sed: `sed -i` no es igual en GNU que
# en BSD y un reemplazo multilinea en sed portable no se puede leer. Python ya es un
# requisito duro de ldt, asi que no agrega dependencia.
if [ -z "$no_rule" ]; then
    : "${agent_file:=$HOME/.claude/CLAUDE.md}"
    LDT_REPO="$repo" LDT_AGENT_FILE="$agent_file" "$py" - <<'PY'
import os
from pathlib import Path

repo = Path(os.environ["LDT_REPO"])
target = Path(os.environ["LDT_AGENT_FILE"]).expanduser()
rule = (repo / "rules" / "ldt.md").read_text(encoding="utf-8").strip()
rule = rule.replace("<RUTA-DEL-REPO>", repo.as_posix())

start, end = "<!-- ldt:start -->", "<!-- ldt:end -->"
block = f"{start}\n{rule}\n{end}"
md = target.read_text(encoding="utf-8") if target.exists() else ""
if start in md and end in md:
    head, _, rest = md.partition(start)
    _, _, tail = rest.partition(end)
    md = head + block + tail
else:
    md = (md.rstrip() + "\n\n" if md.strip() else "") + block + "\n"

target.parent.mkdir(parents=True, exist_ok=True)
# newline="\n" explicito: el archivo lo leen agentes y editores en cualquier plataforma.
target.write_text(md, encoding="utf-8", newline="\n")
print(f"regla: escrita en {target}")
PY
fi

# --- 3. dependencias ----------------------------------------------------------------
echo
"$py" "$repo/ldt.py" doctor
