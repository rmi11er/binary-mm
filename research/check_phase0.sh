#!/usr/bin/env bash
# Phase 0 acceptance check (SPEC.md §7 Phase 0). Exit 0 only if all criteria hold.
set -u
VF=${1:-research/venue_facts.md}
SL=${2:-research/product_shortlist.md}
fail=0

# --- ForecastEx table in venue_facts.md ---
fx=$(awk '/^## ForecastEx/{on=1;next} /^## /{on=0} on' "$VF")
open=$(printf '%s\n' "$fx" | grep -c '\[?\]')
echo "ForecastEx [?] remaining: $open"
if [ "$open" -ne 0 ]; then
  printf '%s\n' "$fx" | grep '\[?\]' | cut -d'|' -f2 | sed 's/^/  OPEN:/'
  fail=1
fi

while IFS= read -r row; do
  item=$(printf '%s' "$row" | cut -d'|' -f2)
  status=$(printf '%s' "$row" | cut -d'|' -f3)
  detail=$(printf '%s' "$row" | cut -d'|' -f4)
  src=$(printf '%s' "$row" | cut -d'|' -f5)
  case "$status" in
    *'[V]'*|*'[S]'*)
      printf '%s' "$src" | grep -q 'https://' || { echo "  NO URL:$item"; fail=1; }
      printf '%s' "$src" | grep -q 'accessed 2026-' || { echo "  NO ACCESS DATE:$item"; fail=1; } ;;
    *'needs owner'*)
      printf '%s' "$detail" | grep -q 'Searched:' || { echo "  NEEDS-OWNER WITHOUT 'Searched:':$item"; fail=1; } ;;
    *'[?]'*) : ;;  # already reported above
    *) echo "  BAD STATUS:$item"; fail=1 ;;
  esac
done < <(printf '%s\n' "$fx" | grep '^| ' | grep -v '^| Item ')

# --- product_shortlist.md ---
if [ ! -f "$SL" ]; then
  echo "MISSING: $SL"; fail=1
else
  n=$(grep -c '^### ' "$SL")
  echo "Shortlist candidates: $n"
  [ "$n" -ge 2 ] || { echo "  FEWER THAN 2 CANDIDATES"; fail=1; }
  for f in 'Venue:' 'Reference price:' 'Reference time:' 'Strike grid:' \
           'Incentive eligibility:' 'Hedge:' 'Market data:' 'Decision:' 'Sources:'; do
    c=$(grep -c "^- $f" "$SL")
    [ "$c" -eq "$n" ] || { echo "  FIELD '$f' present $c/$n"; fail=1; }
  done
  d=$(grep -cE '^- Decision: (GO|NO-GO|NO-GO \(v1\)) — ' "$SL")
  [ "$d" -eq "$n" ] || { echo "  DECISION malformed in $((n-d)) candidate(s)"; fail=1; }
  awk '/^### /{if(name!="" && !url) print "  NO URL in candidate: " name; name=$0; url=0}
       /https:\/\//{url=1}
       END{if(name!="" && !url) print "  NO URL in candidate: " name}' "$SL" | grep . && fail=1
fi

[ "$fail" -eq 0 ] && echo "PHASE 0 ACCEPTANCE: PASS" || echo "PHASE 0 ACCEPTANCE: FAIL"
exit "$fail"
