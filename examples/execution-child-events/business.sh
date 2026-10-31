#!/usr/bin/env bash
# A complete standalone business script; observability is optional.
if [[ -n ${SLEDRA_EVENTS_LIBRARY:-} && -r $SLEDRA_EVENTS_LIBRARY ]]; then
    source "$SLEDRA_EVENTS_LIBRARY"
else
    sledra_event_attach() { return 0; }
    sledra_event_emit() { return 0; }
    sledra_event_detach() { return 0; }
fi
sledra_event_attach
for lead in 0 3 6 9 12; do
    printf 'synthetic ready:%03d\n' "$lead"
    sledra_event_emit data.ready --int lead "$lead"
done
sledra_event_detach
