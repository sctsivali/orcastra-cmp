const gib = 1024 ** 3;

function iso(offsetMs = 0) {
  return new Date(Date.now() + offsetMs).toISOString();
}

function nodeResources() {
  return {
    cpu_total: 32,
    cpu_used: 8,
    cpu_available: 24,
    cpu_allocated: 8,
    cpu_allocation_ratio: 0.25,
    cpu_overcommitted: false,
    cpu_sockets: 1,
    memory_total: 128 * gib,
    memory_used: 32 * gib,
    memory_available: 96 * gib,
    gpu_total: 1,
    gpu_used: 1,
    gpu_available: 0,
    gpu_cards_total: 1,
    gpu_integrated_total: 0,
    gpu_display_total: 0,
    gpu_unknown_total: 0,
    gpu_vendors: ["NVIDIA"],
    gpu_models: ["NVIDIA L4"],
    gpu_utilization_supported: false,
    gpu_cards: [
      {
        index: 0,
        id: 0,
        driver: "nvidia",
        driver_version: "550.54",
        pci_address: "0000:01:00.0",
        vendor: "NVIDIA",
        vendor_id: "10de",
        product: "L4",
        product_id: "27b8",
        numa_node: 0,
        in_use: true,
        used_by: "/1.0/instances/tenant-owned-vm?project=tenant-a",
        gpu_class: "compute",
        is_compute: true,
        is_integrated: false,
        attachable: true,
        model_name: "NVIDIA L4",
        memory_total: 24 * gib,
        memory_shared: false,
        mdev_profiles: [],
      },
    ],
    storage_total: 2 * 1024 ** 4,
    storage_used: 600 * gib,
    storage_available: 2 * 1024 ** 4 - 600 * gib,
    storage_raw_total: 2 * 1024 ** 4,
    storage_raw_used: 600 * gib,
    storage_pools: [
      {
        name: "fast-pool",
        driver: "zfs",
        space_total: 2 * 1024 ** 4,
        space_used: 600 * gib,
        space_available: 2 * 1024 ** 4 - 600 * gib,
      },
    ],
    degraded: false,
    degraded_parts: [],
    hardware: {
      scope: "node",
      member_count: 1,
      detected: true,
      system: {
        vendor: "Acme Compute",
        product: "Atlas 1U",
        family: "Atlas",
        version: "1",
        type: "server",
        firmware: { vendor: "Acme", version: "1.2.3", date: "2026-09-01" },
        chassis: { vendor: "Acme", type: "rack", version: "1" },
        motherboard: { vendor: "Acme", product: "Atlas Board", version: "1" },
      },
      cpu: {
        architectures: ["x86_64"],
        total: 32,
        cores: 16,
        threads: 32,
        numa_nodes: 1,
        sockets: [
          {
            socket: 0,
            name: "Xeon Test CPU",
            vendor: "Intel",
            cores: 16,
            threads: 32,
            frequency: 2400,
            frequency_turbo: 3400,
          },
        ],
      },
      memory: {
        total: 128 * gib,
        used: 32 * gib,
        free: 96 * gib,
        hugepages_total: 0,
        hugepages_used: 0,
        hugepages_size: 0,
      },
      networks: [],
      storage_devices: [
        {
          id: "nvme0n1",
          device: "/dev/nvme0n1",
          model: "Fixture NVMe",
          type: "nvme",
          size: 2 * 1024 ** 4,
          numa_node: 0,
          removable: false,
          read_only: false,
          firmware_version: "1.0",
          block_size: 4096,
          rpm: 0,
          partitions: 2,
        },
      ],
      storage_types: ["NVMe"],
    },
    collected_at: iso(),
  };
}

// The backend's release verdict for a cluster (schemas.HypervisorRelease). The card renders only
// this: it knows no version numbers of its own.
function hypervisorRelease(status = "latest", overrides = {}) {
  const base = {
    status,
    reason: null,
    engine: "LXD",
    track: "6.x",
    lts: false,
    track_retired: false,
    latest_version: "6.10.1",
    released_at: "2026-10-29T01:10:05+00:00",
    channel: "6/stable",
    newer_track: null,
    checked_at: iso(),
    attempted_at: iso(),
  };
  if (status === "not_checked") {
    Object.assign(base, { reason: "feed_unreachable", track: null, lts: null, latest_version: null, released_at: null, channel: null, checked_at: null });
  }
  return { ...base, ...overrides };
}

// `serverVersion` and `release` move cluster-a off the latest LXD so the card offers its update
// instead; `tags` gives cluster-a tags for the tag chip and the Tags facet.
function platformNodes({ serverVersion = "6.10.1", release, tags = [] } = {}) {
  return {
    all_connected: false,
    collected_at: iso(),
    nodes: [
      {
        name: "cluster-a",
        display_name: "Core Cluster",
        endpoint: "https://cluster-a.invalid:8443",
        connected: true,
        reachable: true,
        status: "online",
        node_type: "LXD",
        clustered: false,
        server_version: serverVersion,
        api_version: "1.0",
        hypervisor_release: release === undefined ? hypervisorRelease() : release,
        auth: "trusted",
        error: null,
        tags,
        environment: {
          architectures: ["x86_64"],
          kernel: "Linux",
          kernel_architecture: "x86_64",
          kernel_version: "6.8.0",
          os_name: "Ubuntu",
          os_version: "24.04",
          server: "lxd",
          server_version: serverVersion,
          driver: "lxc",
          driver_version: "6.0",
          storage: "zfs",
          storage_version: "2.2",
          firewall: "nftables",
          instance_types: ["container", "virtual-machine"],
          storage_supported_drivers: [{ Name: "zfs", Version: "2.2", Remote: false }],
        },
        resources: nodeResources(),
      },
      {
        name: "cluster-unreachable",
        display_name: "Edge Cluster",
        endpoint: "https://edge.invalid:8443",
        connected: false,
        reachable: false,
        status: "offline",
        node_type: "LXD",
        clustered: null,
        server_version: null,
        api_version: null,
        auth: null,
        environment: null,
        error: "Connection timed out",
        resources: null,
        tags: [],
      },
    ],
  };
}

function platformAvailability() {
  const generatedAt = iso();
  const clusters = platformNodes().nodes.map((node) => ({
    name: node.name,
    display_name: node.display_name,
    endpoint: node.endpoint,
    status: node.status,
    reason_code: node.reachable ? "ok" : "unreachable",
    reason: node.error,
    node_type: node.node_type,
    checked_at: generatedAt,
    available: Boolean(node.connected && node.reachable),
  }));
  return {
    clusters,
    online_count: clusters.filter((cluster) => cluster.available).length,
    total_count: clusters.length,
    source: "nodes_cache",
    generated_at: generatedAt,
  };
}

function tenantSpecifications({ empty = false } = {}) {
  return {
    total: empty ? 0 : 1,
    collected_at: iso(),
    // The UI must disclose that the result is partial without disclosing this cluster id.
    unreachable_nodes: ["customer-secret-cluster"],
    instances: empty
      ? []
      : [
          {
            name: "tenant-owned-vm",
            host: "cluster-a",
            project: "tenant-a",
            // Lowercase as the API sends it; the display label must still render in Title Case.
            status: "running",
            type: "virtual-machine",
            architecture: "x86_64",
            cpu: { value: "4", source: "explicit" },
            memory: { value: "8GiB", source: "inherited" },
            root_disk: { value: "80GiB", pool: "fast-pool", source: "explicit" },
            gpu_count: 1,
            gpu_devices: ["gpu0"],
          },
        ],
  };
}

function fullInstance(status = "Running") {
  return {
    name: "tenant-owned-vm",
    status,
    status_code: status === "Running" ? 103 : 102,
    ip_addresses: ["10.10.0.20"],
    host: "cluster-a",
    host_endpoint: "https://cluster-a.invalid:8443",
    location: "member-1",
    project: "tenant-a",
    architecture: "x86_64",
    created_at: "2026-09-18T09:00:00Z",
    last_used_at: "2026-09-19T09:00:00Z",
    description: "Tenant workload",
    type: "virtual-machine",
    resources: null,
    config: {
      "limits.cpu": "4",
      "limits.memory": "8GiB",
      "volatile.base_image": "fixture-image-fingerprint-that-remains-complete",
    },
    devices: { root: { type: "disk", path: "/", pool: "fast-pool", size: "80GiB" } },
    expanded_devices: {
      root: { type: "disk", path: "/", pool: "fast-pool", size: "80GiB" },
      eth0: { type: "nic", network: "tenant-net" },
    },
    profiles: ["default"],
    tags: ["tenant"],
  };
}

function instanceMetrics(call, instanceStatus = "Running") {
  if (instanceStatus !== "Running") {
    return {
      instance_name: "tenant-owned-vm",
      host: "cluster-a",
      status: instanceStatus,
      cpu_usage_percent: 0,
      memory_used_bytes: 0,
      memory_total_bytes: 0,
      memory_usage_percent: 0,
      disk_usage_bytes: {},
      network_rx_bytes: {},
      network_tx_bytes: {},
      processes: 0,
      uptime_seconds: 0,
      base_image: null,
      ipv4_addresses: [],
      ipv6_addresses: [],
      pid: null,
      cpu_sample_status: "not_applicable",
      unavailable_sections: [],
      timestamp: iso(),
    };
  }
  const calculating = call === 1;
  return {
    instance_name: "tenant-owned-vm",
    host: "cluster-a",
    status: "Running",
    cpu_usage_percent: calculating ? 0 : 22.5,
    memory_used_bytes: 3 * gib,
    memory_total_bytes: 8 * gib,
    memory_usage_percent: 37.5,
    disk_usage_bytes: { root: 24 * gib },
    network_rx_bytes: { eth0: 1024 * 1024 },
    network_tx_bytes: { eth0: 512 * 1024 },
    processes: 42,
    uptime_seconds: 7200,
    base_image: "fixture-image",
    ipv4_addresses: ["10.10.0.20"],
    ipv6_addresses: [],
    pid: 1234,
    cpu_sample_status: calculating ? "calculating" : "ready",
    unavailable_sections: [],
    // An old server timestamp: freshness is the browser's receive time, so a
    // server clock behind the browser must not mark a reading that just arrived as stale.
    timestamp: calculating ? iso(-20_000) : iso(),
  };
}

function monitoringDashboard() {
  const consumer = {
    name: "tenant-owned-vm",
    host: "cluster-a",
    project: "tenant-a",
    type: "virtual-machine",
    status: "running",
    cpu_usage: 22.5,
    cpu_status: "ready",
    cpu_busy_vcpus: 0.9,
    cpu_vcpus: 4,
    memory_usage_bytes: 3 * gib,
    memory_limit_bytes: 8 * gib,
    disk_usage_bytes: 24 * gib,
    network_rx_bytes: 1024 * 1024,
    network_tx_bytes: 512 * 1024,
    network_rx_bytes_per_sec: 2048,
    network_tx_bytes_per_sec: 1024,
  };
  return {
    timestamp: iso(),
    total_nodes: 1,
    online_nodes: 1,
    inactive_nodes: 0,
    clusters_reporting: 1,
    clusters_total: 1,
    total_instances: 1,
    running_instances: 1,
    stopped_instances: 0,
    other_instances: 0,
    container_count: 0,
    vm_count: 1,
    total_cpu: 32,
    used_cpu: 4,
    cpu_usage_percent: 12.5,
    realtime_cpu_percent: 2.81,
    cpu_busy_threads: 0.9,
    cpu_busy_percent: 2.81,
    cpu_status: "ready",
    cpu_reporting_instances: 1,
    cpu_reporting_threads: 32,
    total_memory_bytes: 128 * gib,
    used_memory_bytes: 32 * gib,
    memory_usage_percent: 25,
    realtime_memory_bytes: 3 * gib,
    realtime_memory_percent: 2.34,
    total_storage_bytes: 2 * 1024 ** 4,
    used_storage_bytes: 600 * gib,
    storage_usage_percent: 29.3,
    total_gpu: 1,
    total_gpu_cards: 1,
    used_gpu: 1,
    nodes: [
      {
        node_name: "cluster-a",
        node_type: "LXD",
        is_online: true,
        cluster_status: "online",
        status_message: null,
        cpu_total: 32,
        cpu_used: 4,
        cpu_usage_percent: 12.5,
        cpu_allocated: 4,
        cpu_allocation_ratio: 0.125,
        cpu_overcommitted: false,
        memory_total_bytes: 128 * gib,
        memory_used_bytes: 32 * gib,
        memory_usage_percent: 25,
        storage_total_bytes: 2 * 1024 ** 4,
        storage_used_bytes: 600 * gib,
        storage_usage_percent: 29.3,
        gpu_total: 1,
        gpu_used: 1,
        gpu_available: 0,
        gpu_cards_total: 1,
        gpu_integrated_total: 0,
        gpu_models: ["NVIDIA L4"],
        gpu_utilization_supported: false,
        instance_count: 1,
        running_instances: 1,
        container_count: 0,
        vm_count: 1,
        cpu_busy_threads: 0.9,
        cpu_busy_percent: 2.81,
        cpu_status: "ready",
        cpu_reporting_instances: 1,
      },
    ],
    storage_pools: [
      {
        name: "fast-pool",
        node: "cluster-a",
        // As resources.py sends it for a standalone host: the host is its own one member.
        members: ["cluster-a"],
        scope: "local",
        driver: "zfs",
        total_bytes: 2 * 1024 ** 4,
        used_bytes: 600 * gib,
        available_bytes: 2 * 1024 ** 4 - 600 * gib,
        usage_percent: 29.3,
      },
    ],
    consumers: [consumer],
    top_cpu_consumers: [consumer],
    top_memory_consumers: [consumer],
    warnings: { total: 0, critical: 0, warning: 0, info: 0 },
    collection_time_ms: 125,
  };
}

// One point per 30 s collection, as the collector writes them, over the requested window.
function monitoringHistory(windowSeconds = 300) {
  const count = Math.min(Math.max(Math.floor((Number(windowSeconds) || 300) / 30), 2), 48);
  return Array.from({ length: count }, (_, index) => ({
    timestamp: iso((index - count) * 30_000),
    cpu_usage_percent: index === 0 ? null : 10 + index * 0.5,
    memory_usage_percent: 25 + index * 0.25,
    storage_usage_percent: 29.3,
    network_rx_bytes: 0,
    network_tx_bytes: 0,
    network_rx_bytes_per_sec: 2000 + index * 10,
    network_tx_bytes_per_sec: 1000 + index * 10,
    disk_io_bytes: 0,
    running_instances: 1,
    nodes: {
      "cluster-a": {
        cpu_usage_percent: index === 0 ? null : 10 + index * 0.5,
        memory_usage_percent: 25 + index * 0.25,
        storage_usage_percent: 29.3,
        network_rx_bytes_per_sec: 2000 + index * 10,
        network_tx_bytes_per_sec: 1000 + index * 10,
        running_instances: 1,
      },
    },
  }));
}

module.exports = {
  fullInstance,
  hypervisorRelease,
  instanceMetrics,
  iso,
  monitoringDashboard,
  monitoringHistory,
  platformAvailability,
  platformNodes,
  tenantSpecifications,
};
