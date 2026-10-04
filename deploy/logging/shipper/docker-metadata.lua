-- Adds container metadata to records tailed from /var/lib/docker/containers/<id>/<id>-json.log.
-- Reads the container's config.v2.json once per container id (cached). LuaJIT in Fluent Bit has no
-- JSON library, so the few fields needed are extracted with patterns that are unambiguous in that
-- file: the container name is the only "Name" value starting with "/", compose labels are unique
-- keys, and the image name is read from inside the "Config" object.
local cache = {}
local SELF = os.getenv("SHIPPER_CONTAINER_NAME") or "log-shipper"

local function meta(id)
  local m = cache[id]
  if m then return m end
  local f = io.open("/var/lib/docker/containers/" .. id .. "/config.v2.json", "r")
  if not f then return nil end
  local s = f:read("*a"); f:close()
  local config = s:match('"Config":(%b{})') or ""
  m = {
    name = s:match('"Name":"/([^"]+)"'),
    project = s:match('"com%.docker%.compose%.project":"([^"]*)"'),
    service = s:match('"com%.docker%.compose%.service":"([^"]*)"'),
    image = config:match('"Image":"([^"]*)"'),
  }
  cache[id] = m
  return m
end

function add_docker_metadata(tag, ts, record)
  local id = tag:match("(%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x%x)")
  if not id then return 0, ts, record end
  local m = meta(id)
  if m and m.name == SELF then return -1, ts, record end  -- never ship our own output (feedback loop)
  record["container_id"] = id
  if m then
    record["container_name"] = m.name
    record["compose_project"] = m.project
    record["compose_service"] = m.service
    record["image"] = m.image
  end
  if record["stream"] then record["source"] = record["stream"]; record["stream"] = nil end
  if type(record["log"]) == "string" then record["log"] = record["log"]:gsub("\n$", "") end
  return 2, ts, record
end
