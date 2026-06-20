ui = true

storage "raft" {
  path    = "/vault/data"
  node_id = "orcastra-mini"
}

listener "tcp" {
  address         = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  # TLS is disabled here because Vault is only reachable on the private Docker network
  # (and 127.0.0.1 on the host for init/unseal). Front it with nginx/TLS if exposed.
  tls_disable = 1
}

api_addr     = "http://vault:8200"
cluster_addr = "http://vault:8201"
disable_mlock = false
