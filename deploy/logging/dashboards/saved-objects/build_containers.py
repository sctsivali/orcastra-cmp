#!/usr/bin/env python3
"""Generate containers.ndjson: the "Container Logs" dashboard and its visualizations/searches.

Run after editing, commit both files: python3 build_containers.py > containers.ndjson
Imported into the Global tenant by scripts/dashboards-objects.sh. Visualization params follow
the shape OpenSearch Dashboards 3.x stores (copied from the Orcastra dashboards that ship with it).
"""
import json

IP = "containers-all"  # index pattern id for containers-*
ERR = "level:(error or fatal)"


def ssj(query="", index=True):
    s = {"query": {"query": query, "language": "kuery"}, "filter": []}
    if index:
        s["indexRefName"] = "kibanaSavedObjectMeta.searchSourceJSON.index"
    return json.dumps(s)


def ref():
    return [{"name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern", "id": IP}]


def vis(vid, title, vtype, params, aggs, query="", desc="", extra_refs=None):
    return {"type": "visualization", "id": vid, "references": ref() + (extra_refs or []),
            "attributes": {"title": title, "description": desc, "version": 1, "uiStateJSON": "{}",
                           "visState": json.dumps({"title": title, "type": vtype, "params": params, "aggs": aggs}),
                           "kibanaSavedObjectMeta": {"searchSourceJSON": ssj(query)}}}


def count(label=""):
    return {"id": "1", "enabled": True, "type": "count", "schema": "metric", "params": {"customLabel": label} if label else {}}


def terms(aid, field, size, schema="segment", label=""):
    p = {"field": field, "size": size, "order": "desc", "orderBy": "1", "otherBucket": False, "missingBucket": False}
    if label:
        p["customLabel"] = label
    return {"id": aid, "enabled": True, "type": "terms", "schema": schema, "params": p}


def metric_params(label_size=36):
    return {"addTooltip": True, "addLegend": False, "type": "metric", "metric": {
        "percentageMode": False, "useRanges": False, "colorSchema": "Green to Red", "metricColorMode": "None",
        "colorsRange": [{"from": 0, "to": 10000}], "labels": {"show": True}, "invertColors": False,
        "style": {"bgFill": "#000", "bgColor": False, "labelColor": False, "subText": "", "fontSize": label_size}}}


def xy(vtype, mode, label):
    return {"type": vtype, "grid": {"categoryLines": False},
            "categoryAxes": [{"id": "CategoryAxis-1", "type": "category", "position": "bottom", "show": True,
                              "style": {}, "scale": {"type": "linear"}, "labels": {"show": True, "filter": True, "truncate": 100}, "title": {}}],
            "valueAxes": [{"id": "ValueAxis-1", "name": "LeftAxis-1", "type": "value", "position": "left", "show": True,
                           "style": {}, "scale": {"type": "linear", "mode": "normal"}, "labels": {"show": True, "rotate": 0, "filter": False, "truncate": 100}, "title": {"text": label}}],
            "seriesParams": [{"show": True, "type": vtype if vtype != "horizontal_bar" else "histogram", "mode": mode,
                              "data": {"label": label, "id": "1"}, "valueAxis": "ValueAxis-1", "drawLinesBetweenPoints": True,
                              "lineWidth": 2, "showCircles": True}],
            "addTooltip": True, "addLegend": True, "legendPosition": "right", "times": [], "addTimeMarker": False,
            "labels": {"show": False}, "thresholdLine": {"show": False, "value": 10, "width": 1, "style": "full", "color": "#E7664C"}}


def hbar(label):
    p = xy("histogram", "normal", label)
    p["type"] = "histogram"
    p["categoryAxes"][0].update({"position": "left", "labels": {"show": True, "rotate": 0, "filter": False, "truncate": 200}})
    p["valueAxes"][0].update({"position": "bottom", "name": "BottomAxis-1"})
    p["addLegend"] = False
    return p


objs = []

# Filter controls (stack / env / host / container / level).
controls, crefs = [], []
for i, (field, label) in enumerate([("stack", "Stack"), ("env", "Environment"), ("host", "Host"),
                                    ("container_name", "Container"), ("level", "Level")]):
    controls.append({"id": str(i), "fieldName": field, "label": label, "type": "list", "parent": "",
                     "indexPatternRefName": f"control_{i}_index_pattern",
                     "options": {"type": "terms", "multiselect": True, "dynamicOptions": True, "size": 100, "order": "desc"}})
    crefs.append({"name": f"control_{i}_index_pattern", "type": "index-pattern", "id": IP})
objs.append({"type": "visualization", "id": "containers-controls", "references": crefs, "attributes": {
    "title": "[Containers] Filters", "description": "", "version": 1, "uiStateJSON": "{}",
    "visState": json.dumps({"title": "[Containers] Filters", "type": "input_control_vis", "aggs": [],
                            "params": {"controls": controls, "updateFiltersOnChange": True, "useTimeFilter": True, "pinFilters": False}}),
    "kibanaSavedObjectMeta": {"searchSourceJSON": ssj(index=False)}}})

objs.append(vis("containers-total", "[Containers] Log lines", "metric", metric_params(), [count("Log lines")]))
objs.append(vis("containers-errors-count", "[Containers] Errors", "metric", metric_params(), [count("Error / fatal lines")], query=ERR))
objs.append(vis("containers-count", "[Containers] Containers", "metric", metric_params(),
                [{"id": "1", "enabled": True, "type": "cardinality", "schema": "metric", "params": {"field": "container_name", "customLabel": "Containers"}}]))
objs.append(vis("containers-hosts", "[Containers] Hosts", "metric", metric_params(),
                [{"id": "1", "enabled": True, "type": "cardinality", "schema": "metric", "params": {"field": "host", "customLabel": "Hosts"}}]))
objs.append(vis("containers-volume", "[Containers] Log volume by stack", "area", xy("area", "stacked", "Log lines"),
                [count(), {"id": "2", "enabled": True, "type": "date_histogram", "schema": "segment",
                           "params": {"field": "@timestamp", "interval": "auto", "min_doc_count": 1, "extended_bounds": {}}},
                 terms("3", "stack", 10, schema="group")]))
objs.append(vis("containers-errors-time", "[Containers] Errors over time", "histogram", xy("histogram", "stacked", "Error lines"),
                [count(), {"id": "2", "enabled": True, "type": "date_histogram", "schema": "segment",
                           "params": {"field": "@timestamp", "interval": "auto", "min_doc_count": 1, "extended_bounds": {}}},
                 terms("3", "container_name", 10, schema="group")], query=ERR))
objs.append(vis("containers-levels", "[Containers] Levels", "pie",
                {"type": "pie", "addTooltip": True, "addLegend": True, "legendPosition": "right", "isDonut": True,
                 "labels": {"show": False, "values": True, "last_level": True, "truncate": 100}},
                [count(), terms("2", "level", 10)]))
objs.append(vis("containers-top", "[Containers] Busiest containers", "horizontal_bar", hbar("Log lines"),
                [count(), terms("2", "container_name", 20)]))
objs.append(vis("containers-error-table", "[Containers] Errors by container", "table",
                {"perPage": 10, "showPartialRows": False, "showMetricsAtAllLevels": False, "showTotal": True,
                 "totalFunc": "sum", "percentageCol": "", "sort": {"columnIndex": None, "direction": None}},
                [count("Errors"), terms("2", "stack", 20, schema="bucket", label="Stack"),
                 terms("3", "container_name", 50, schema="bucket", label="Container")], query=ERR))


def search(sid, title, query, desc):
    return {"type": "search", "id": sid, "references": ref(), "attributes": {
        "title": title, "description": desc, "version": 1,
        "columns": ["stack", "env", "host", "container_name", "level", "message"], "sort": [["@timestamp", "desc"]],
        "kibanaSavedObjectMeta": {"searchSourceJSON": ssj(query)}}}


objs.append(search("containers-logs", "Container logs", "", "Every container line from every stack (containers-*)"))
objs.append(search("containers-errors", "Container errors", ERR, "Error and fatal lines from every stack (containers-*)"))

layout = [  # (id, type, x, y, w, h)
    ("containers-controls", "visualization", 0, 0, 48, 5),
    ("containers-total", "visualization", 0, 5, 12, 6), ("containers-errors-count", "visualization", 12, 5, 12, 6),
    ("containers-count", "visualization", 24, 5, 12, 6), ("containers-hosts", "visualization", 36, 5, 12, 6),
    ("containers-volume", "visualization", 0, 11, 32, 12), ("containers-levels", "visualization", 32, 11, 16, 12),
    ("containers-top", "visualization", 0, 23, 24, 14), ("containers-error-table", "visualization", 24, 23, 24, 14),
    ("containers-errors-time", "visualization", 0, 37, 48, 10),
    ("containers-errors", "search", 0, 47, 48, 14),
    ("containers-logs", "search", 0, 61, 48, 20),
]
panels, prefs = [], []
for i, (oid, otype, x, y, w, h) in enumerate(layout, 1):
    panels.append({"version": "3.0.0", "gridData": {"x": x, "y": y, "w": w, "h": h, "i": str(i)},
                   "panelIndex": str(i), "embeddableConfig": {}, "panelRefName": f"panel_{i}"})
    prefs.append({"name": f"panel_{i}", "type": otype, "id": oid})
objs.append({"type": "dashboard", "id": "containers-overview", "references": prefs, "attributes": {
    "title": "Container Logs", "hits": 0, "version": 1, "timeRestore": True, "timeFrom": "now-1h", "timeTo": "now",
    "refreshInterval": {"pause": False, "value": 30000},
    "description": "Docker container logs from every stack (containers-*): volume, levels, errors, and the raw lines. Use the filters at the top.",
    "optionsJSON": json.dumps({"hidePanelTitles": False, "useMargins": True}), "panelsJSON": json.dumps(panels),
    "kibanaSavedObjectMeta": {"searchSourceJSON": ssj(index=False)}}})

for o in objs:
    print(json.dumps(o))
