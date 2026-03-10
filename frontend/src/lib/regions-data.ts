/**
 * Comprehensive global regions data for Orcastra Dashboard
 * Enterprise-grade region/country/city database with coordinates
 * ISO 3166-1 compliant country codes
 */

// Major cloud regions based on global datacenter locations
export interface Region {
  id: string;
  name: string;
  country_code: string;
  country_name: string;
  city: string;
  latitude: number;
  longitude: number;
  timezone: string;
  continent: string;
}

export interface Country {
  code: string; // ISO 3166-1 alpha-2
  name: string;
  continent: string;
  regions: Region[];
}

export interface Continent {
  id: string;
  name: string;
  countries: Country[];
}

// All continents
export const CONTINENTS: Record<string, string> = {
  AF: "Africa",
  AS: "Asia",
  EU: "Europe",
  NA: "North America",
  SA: "South America",
  OC: "Oceania",
  AN: "Antarctica",
};

// Comprehensive list of countries with major cities/datacenters
// Organized by continent for easy navigation
export const REGIONS_DATA: Region[] = [
  // ==================== ASIA ====================
  
  // ==================== INDONESIA - 98 Kota/Kabupaten ====================
  // Based on: https://id.wikipedia.org/wiki/Daftar_kota_di_Indonesia
  // 93 kota otonom + 5 kota administrasi DKI Jakarta
  // TODO: match with this (FROM BPS) https://raw.githubusercontent.com/benangmerah/wilayah/refs/heads/master/datasources/daftar-nama-daerah.csv
  
  // === ACEH (WIB) ===
  { id: "id-bna", name: "Banda Aceh", country_code: "ID", country_name: "Indonesia", city: "Banda Aceh", latitude: 5.5483, longitude: 95.3238, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-sab", name: "Sabang", country_code: "ID", country_name: "Indonesia", city: "Sabang", latitude: 5.8933, longitude: 95.3164, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-lgs", name: "Langsa", country_code: "ID", country_name: "Indonesia", city: "Langsa", latitude: 4.4681, longitude: 97.9683, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-lsm", name: "Lhokseumawe", country_code: "ID", country_name: "Indonesia", city: "Lhokseumawe", latitude: 5.1801, longitude: 97.1507, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-sbs", name: "Subulussalam", country_code: "ID", country_name: "Indonesia", city: "Subulussalam", latitude: 2.6419, longitude: 98.0006, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === SUMATERA UTARA (WIB) ===
  { id: "id-mdn", name: "Medan", country_code: "ID", country_name: "Indonesia", city: "Medan", latitude: 3.5952, longitude: 98.6722, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-bnj", name: "Binjai", country_code: "ID", country_name: "Indonesia", city: "Binjai", latitude: 3.6003, longitude: 98.4854, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-psp", name: "Padang Sidempuan", country_code: "ID", country_name: "Indonesia", city: "Padang Sidempuan", latitude: 1.3793, longitude: 99.2719, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-pms", name: "Pematangsiantar", country_code: "ID", country_name: "Indonesia", city: "Pematangsiantar", latitude: 2.9595, longitude: 99.0687, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-sbg", name: "Sibolga", country_code: "ID", country_name: "Indonesia", city: "Sibolga", latitude: 1.7421, longitude: 98.7792, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-tjb", name: "Tanjungbalai", country_code: "ID", country_name: "Indonesia", city: "Tanjungbalai", latitude: 2.9667, longitude: 99.8000, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-tbt", name: "Tebing Tinggi", country_code: "ID", country_name: "Indonesia", city: "Tebing Tinggi", latitude: 3.3264, longitude: 99.1625, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-gst", name: "Gunungsitoli", country_code: "ID", country_name: "Indonesia", city: "Gunungsitoli", latitude: 1.2892, longitude: 97.6167, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === SUMATERA BARAT (WIB) ===
  { id: "id-pdg", name: "Padang", country_code: "ID", country_name: "Indonesia", city: "Padang", latitude: -0.9471, longitude: 100.4172, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-bkt", name: "Bukittinggi", country_code: "ID", country_name: "Indonesia", city: "Bukittinggi", latitude: -0.3094, longitude: 100.3681, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-ppj", name: "Padang Panjang", country_code: "ID", country_name: "Indonesia", city: "Padang Panjang", latitude: -0.4583, longitude: 100.4167, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-pyn", name: "Payakumbuh", country_code: "ID", country_name: "Indonesia", city: "Payakumbuh", latitude: -0.2217, longitude: 100.6308, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-prm", name: "Pariaman", country_code: "ID", country_name: "Indonesia", city: "Pariaman", latitude: -0.6250, longitude: 100.1181, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-swl", name: "Sawahlunto", country_code: "ID", country_name: "Indonesia", city: "Sawahlunto", latitude: -0.6833, longitude: 100.7667, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-slk", name: "Solok", country_code: "ID", country_name: "Indonesia", city: "Solok", latitude: -0.7897, longitude: 100.6544, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === RIAU (WIB) ===
  { id: "id-pkb", name: "Pekanbaru", country_code: "ID", country_name: "Indonesia", city: "Pekanbaru", latitude: 0.5071, longitude: 101.4478, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-dmi", name: "Dumai", country_code: "ID", country_name: "Indonesia", city: "Dumai", latitude: 1.6667, longitude: 101.4500, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === KEPULAUAN RIAU (WIB) ===
  { id: "id-btm", name: "Batam", country_code: "ID", country_name: "Indonesia", city: "Batam", latitude: 1.0456, longitude: 104.0305, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-tjp", name: "Tanjungpinang", country_code: "ID", country_name: "Indonesia", city: "Tanjungpinang", latitude: 0.9167, longitude: 104.4500, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === JAMBI (WIB) ===
  { id: "id-jbi", name: "Jambi", country_code: "ID", country_name: "Indonesia", city: "Jambi", latitude: -1.6101, longitude: 103.6131, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-sph", name: "Sungai Penuh", country_code: "ID", country_name: "Indonesia", city: "Sungai Penuh", latitude: -2.0833, longitude: 101.3833, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === SUMATERA SELATAN (WIB) ===
  { id: "id-plm", name: "Palembang", country_code: "ID", country_name: "Indonesia", city: "Palembang", latitude: -2.9761, longitude: 104.7754, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-lbl", name: "Lubuklinggau", country_code: "ID", country_name: "Indonesia", city: "Lubuklinggau", latitude: -3.2975, longitude: 102.8614, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-pga", name: "Pagar Alam", country_code: "ID", country_name: "Indonesia", city: "Pagar Alam", latitude: -4.0233, longitude: 103.2517, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-prb", name: "Prabumulih", country_code: "ID", country_name: "Indonesia", city: "Prabumulih", latitude: -3.4286, longitude: 104.2306, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === BENGKULU (WIB) ===
  { id: "id-bku", name: "Bengkulu", country_code: "ID", country_name: "Indonesia", city: "Bengkulu", latitude: -3.7928, longitude: 102.2608, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === LAMPUNG (WIB) ===
  { id: "id-blp", name: "Bandar Lampung", country_code: "ID", country_name: "Indonesia", city: "Bandar Lampung", latitude: -5.4294, longitude: 105.2625, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-met", name: "Metro", country_code: "ID", country_name: "Indonesia", city: "Metro", latitude: -5.1133, longitude: 105.3067, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === KEPULAUAN BANGKA BELITUNG (WIB) ===
  { id: "id-pgp", name: "Pangkalpinang", country_code: "ID", country_name: "Indonesia", city: "Pangkalpinang", latitude: -2.1333, longitude: 106.1167, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === DKI JAKARTA - 5 Kota Administrasi (WIB) ===
  { id: "id-jkt", name: "Jakarta", country_code: "ID", country_name: "Indonesia", city: "Jakarta", latitude: -6.2088, longitude: 106.8456, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-jkp", name: "Jakarta Pusat", country_code: "ID", country_name: "Indonesia", city: "Jakarta Pusat", latitude: -6.1862, longitude: 106.8346, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-jku", name: "Jakarta Utara", country_code: "ID", country_name: "Indonesia", city: "Jakarta Utara", latitude: -6.1384, longitude: 106.8638, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-jkb", name: "Jakarta Barat", country_code: "ID", country_name: "Indonesia", city: "Jakarta Barat", latitude: -6.1673, longitude: 106.7637, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-jks", name: "Jakarta Selatan", country_code: "ID", country_name: "Indonesia", city: "Jakarta Selatan", latitude: -6.2615, longitude: 106.8106, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-jke", name: "Jakarta Timur", country_code: "ID", country_name: "Indonesia", city: "Jakarta Timur", latitude: -6.2250, longitude: 106.9004, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === BANTEN (WIB) ===
  { id: "id-srg", name: "Serang", country_code: "ID", country_name: "Indonesia", city: "Serang", latitude: -6.1206, longitude: 106.1503, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-clg", name: "Cilegon", country_code: "ID", country_name: "Indonesia", city: "Cilegon", latitude: -6.0167, longitude: 106.0500, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-tng", name: "Tangerang", country_code: "ID", country_name: "Indonesia", city: "Tangerang", latitude: -6.1783, longitude: 106.6319, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-tgs", name: "Tangerang Selatan", country_code: "ID", country_name: "Indonesia", city: "Tangerang Selatan", latitude: -6.2894, longitude: 106.7108, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === JAWA BARAT (WIB) ===
  { id: "id-bdg", name: "Bandung", country_code: "ID", country_name: "Indonesia", city: "Bandung", latitude: -6.9175, longitude: 107.6191, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-bgr", name: "Bogor", country_code: "ID", country_name: "Indonesia", city: "Bogor", latitude: -6.5971, longitude: 106.8060, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-bki", name: "Bekasi", country_code: "ID", country_name: "Indonesia", city: "Bekasi", latitude: -6.2383, longitude: 107.0000, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-dpk", name: "Depok", country_code: "ID", country_name: "Indonesia", city: "Depok", latitude: -6.4025, longitude: 106.7942, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-cmh", name: "Cimahi", country_code: "ID", country_name: "Indonesia", city: "Cimahi", latitude: -6.8722, longitude: 107.5389, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-crb", name: "Cirebon", country_code: "ID", country_name: "Indonesia", city: "Cirebon", latitude: -6.7056, longitude: 108.5550, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-skb", name: "Sukabumi", country_code: "ID", country_name: "Indonesia", city: "Sukabumi", latitude: -6.9178, longitude: 106.9269, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-tsm", name: "Tasikmalaya", country_code: "ID", country_name: "Indonesia", city: "Tasikmalaya", latitude: -7.3506, longitude: 108.2172, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-bjr", name: "Banjar", country_code: "ID", country_name: "Indonesia", city: "Banjar", latitude: -7.3667, longitude: 108.5333, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === JAWA TENGAH (WIB) ===
  { id: "id-smg", name: "Semarang", country_code: "ID", country_name: "Indonesia", city: "Semarang", latitude: -6.9666, longitude: 110.4196, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-ska", name: "Surakarta (Solo)", country_code: "ID", country_name: "Indonesia", city: "Surakarta", latitude: -7.5755, longitude: 110.8243, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-mgl", name: "Magelang", country_code: "ID", country_name: "Indonesia", city: "Magelang", latitude: -7.4797, longitude: 110.2177, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-pkl", name: "Pekalongan", country_code: "ID", country_name: "Indonesia", city: "Pekalongan", latitude: -6.8897, longitude: 109.6753, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-slt", name: "Salatiga", country_code: "ID", country_name: "Indonesia", city: "Salatiga", latitude: -7.3306, longitude: 110.5083, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-tgl", name: "Tegal", country_code: "ID", country_name: "Indonesia", city: "Tegal", latitude: -6.8694, longitude: 109.1402, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === DI YOGYAKARTA (WIB) ===
  { id: "id-yog", name: "Yogyakarta", country_code: "ID", country_name: "Indonesia", city: "Yogyakarta", latitude: -7.7956, longitude: 110.3695, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === JAWA TIMUR (WIB) ===
  { id: "id-sby", name: "Surabaya", country_code: "ID", country_name: "Indonesia", city: "Surabaya", latitude: -7.2575, longitude: 112.7521, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-mlg", name: "Malang", country_code: "ID", country_name: "Indonesia", city: "Malang", latitude: -7.9786, longitude: 112.6317, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-btu", name: "Batu", country_code: "ID", country_name: "Indonesia", city: "Batu", latitude: -7.8672, longitude: 112.5239, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-blt", name: "Blitar", country_code: "ID", country_name: "Indonesia", city: "Blitar", latitude: -8.0983, longitude: 112.1608, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-kdr", name: "Kediri", country_code: "ID", country_name: "Indonesia", city: "Kediri", latitude: -7.8167, longitude: 112.0117, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-mdu", name: "Madiun", country_code: "ID", country_name: "Indonesia", city: "Madiun", latitude: -7.6298, longitude: 111.5300, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-mjk", name: "Mojokerto", country_code: "ID", country_name: "Indonesia", city: "Mojokerto", latitude: -7.4700, longitude: 112.4333, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-psn", name: "Pasuruan", country_code: "ID", country_name: "Indonesia", city: "Pasuruan", latitude: -7.6456, longitude: 112.9072, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-pbl", name: "Probolinggo", country_code: "ID", country_name: "Indonesia", city: "Probolinggo", latitude: -7.7543, longitude: 113.2159, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === BALI (WITA) ===
  { id: "id-dps", name: "Denpasar", country_code: "ID", country_name: "Indonesia", city: "Denpasar", latitude: -8.6705, longitude: 115.2126, timezone: "Asia/Makassar", continent: "AS" },
  
  // === NUSA TENGGARA BARAT (WITA) ===
  { id: "id-mtm", name: "Mataram", country_code: "ID", country_name: "Indonesia", city: "Mataram", latitude: -8.5833, longitude: 116.1167, timezone: "Asia/Makassar", continent: "AS" },
  { id: "id-bim", name: "Bima", country_code: "ID", country_name: "Indonesia", city: "Bima", latitude: -8.4608, longitude: 118.7272, timezone: "Asia/Makassar", continent: "AS" },
  
  // === NUSA TENGGARA TIMUR (WITA) ===
  { id: "id-kpg", name: "Kupang", country_code: "ID", country_name: "Indonesia", city: "Kupang", latitude: -10.1772, longitude: 123.6070, timezone: "Asia/Makassar", continent: "AS" },
  
  // === KALIMANTAN BARAT (WIB) ===
  { id: "id-ptk", name: "Pontianak", country_code: "ID", country_name: "Indonesia", city: "Pontianak", latitude: -0.0263, longitude: 109.3425, timezone: "Asia/Jakarta", continent: "AS" },
  { id: "id-skw", name: "Singkawang", country_code: "ID", country_name: "Indonesia", city: "Singkawang", latitude: 0.9000, longitude: 108.9833, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === KALIMANTAN TENGAH (WIB) ===
  { id: "id-pky", name: "Palangka Raya", country_code: "ID", country_name: "Indonesia", city: "Palangka Raya", latitude: -2.2136, longitude: 113.9108, timezone: "Asia/Jakarta", continent: "AS" },
  
  // === KALIMANTAN SELATAN (WITA) ===
  { id: "id-bjm", name: "Banjarmasin", country_code: "ID", country_name: "Indonesia", city: "Banjarmasin", latitude: -3.3167, longitude: 114.5900, timezone: "Asia/Makassar", continent: "AS" },
  { id: "id-bjb", name: "Banjarbaru", country_code: "ID", country_name: "Indonesia", city: "Banjarbaru", latitude: -3.4586, longitude: 114.8308, timezone: "Asia/Makassar", continent: "AS" },
  
  // === KALIMANTAN TIMUR (WITA) ===
  { id: "id-smr", name: "Samarinda", country_code: "ID", country_name: "Indonesia", city: "Samarinda", latitude: -0.5022, longitude: 117.1536, timezone: "Asia/Makassar", continent: "AS" },
  { id: "id-bpn", name: "Balikpapan", country_code: "ID", country_name: "Indonesia", city: "Balikpapan", latitude: -1.2379, longitude: 116.8529, timezone: "Asia/Makassar", continent: "AS" },
  { id: "id-btg", name: "Bontang", country_code: "ID", country_name: "Indonesia", city: "Bontang", latitude: 0.1333, longitude: 117.5000, timezone: "Asia/Makassar", continent: "AS" },
  
  // === KALIMANTAN UTARA (WITA) ===
  { id: "id-trk", name: "Tarakan", country_code: "ID", country_name: "Indonesia", city: "Tarakan", latitude: 3.3000, longitude: 117.6333, timezone: "Asia/Makassar", continent: "AS" },
  
  // === SULAWESI UTARA (WITA) ===
  { id: "id-mnd", name: "Manado", country_code: "ID", country_name: "Indonesia", city: "Manado", latitude: 1.4748, longitude: 124.8421, timezone: "Asia/Makassar", continent: "AS" },
  { id: "id-bit", name: "Bitung", country_code: "ID", country_name: "Indonesia", city: "Bitung", latitude: 1.4444, longitude: 125.1917, timezone: "Asia/Makassar", continent: "AS" },
  { id: "id-tom", name: "Tomohon", country_code: "ID", country_name: "Indonesia", city: "Tomohon", latitude: 1.3167, longitude: 124.8333, timezone: "Asia/Makassar", continent: "AS" },
  { id: "id-ktm", name: "Kotamobagu", country_code: "ID", country_name: "Indonesia", city: "Kotamobagu", latitude: 0.7333, longitude: 124.3167, timezone: "Asia/Makassar", continent: "AS" },
  
  // === GORONTALO (WITA) ===
  { id: "id-gto", name: "Gorontalo", country_code: "ID", country_name: "Indonesia", city: "Gorontalo", latitude: 0.5333, longitude: 123.0667, timezone: "Asia/Makassar", continent: "AS" },
  
  // === SULAWESI TENGAH (WITA) ===
  { id: "id-plu", name: "Palu", country_code: "ID", country_name: "Indonesia", city: "Palu", latitude: -0.8917, longitude: 119.8707, timezone: "Asia/Makassar", continent: "AS" },
  
  // === SULAWESI SELATAN (WITA) ===
  { id: "id-mks", name: "Makassar", country_code: "ID", country_name: "Indonesia", city: "Makassar", latitude: -5.1477, longitude: 119.4327, timezone: "Asia/Makassar", continent: "AS" },
  { id: "id-prp", name: "Parepare", country_code: "ID", country_name: "Indonesia", city: "Parepare", latitude: -4.0135, longitude: 119.6255, timezone: "Asia/Makassar", continent: "AS" },
  { id: "id-plp", name: "Palopo", country_code: "ID", country_name: "Indonesia", city: "Palopo", latitude: -2.9928, longitude: 120.1972, timezone: "Asia/Makassar", continent: "AS" },
  
  // === SULAWESI TENGGARA (WITA) ===
  { id: "id-kdi", name: "Kendari", country_code: "ID", country_name: "Indonesia", city: "Kendari", latitude: -3.9675, longitude: 122.5947, timezone: "Asia/Makassar", continent: "AS" },
  { id: "id-bbu", name: "Baubau", country_code: "ID", country_name: "Indonesia", city: "Baubau", latitude: -5.4667, longitude: 122.6333, timezone: "Asia/Makassar", continent: "AS" },
  
  // === MALUKU (WIT) ===
  { id: "id-amb", name: "Ambon", country_code: "ID", country_name: "Indonesia", city: "Ambon", latitude: -3.6954, longitude: 128.1814, timezone: "Asia/Jayapura", continent: "AS" },
  { id: "id-tul", name: "Tual", country_code: "ID", country_name: "Indonesia", city: "Tual", latitude: -5.6333, longitude: 132.7333, timezone: "Asia/Jayapura", continent: "AS" },
  
  // === MALUKU UTARA (WIT) ===
  { id: "id-tte", name: "Ternate", country_code: "ID", country_name: "Indonesia", city: "Ternate", latitude: 0.7833, longitude: 127.3833, timezone: "Asia/Jayapura", continent: "AS" },
  { id: "id-tdk", name: "Tidore Kepulauan", country_code: "ID", country_name: "Indonesia", city: "Tidore", latitude: 0.6833, longitude: 127.4000, timezone: "Asia/Jayapura", continent: "AS" },
  
  // === PAPUA (WIT) ===
  { id: "id-jap", name: "Jayapura", country_code: "ID", country_name: "Indonesia", city: "Jayapura", latitude: -2.5333, longitude: 140.7167, timezone: "Asia/Jayapura", continent: "AS" },
  
  // === PAPUA BARAT (WIT) ===
  { id: "id-sor", name: "Sorong", country_code: "ID", country_name: "Indonesia", city: "Sorong", latitude: -0.8833, longitude: 131.2500, timezone: "Asia/Jayapura", continent: "AS" },
  
  // === IKN Nusantara (WITA) ===
  { id: "id-ikn", name: "IKN Nusantara", country_code: "ID", country_name: "Indonesia", city: "Nusantara", latitude: -1.2833, longitude: 116.7667, timezone: "Asia/Makassar", continent: "AS" },
  
  // ==================== END INDONESIA ====================
  
  // [DISABLED] Singapore
  // { id: "sg-sin", name: "Singapore", country_code: "SG", country_name: "Singapore", city: "Singapore", latitude: 1.3521, longitude: 103.8198, timezone: "Asia/Singapore", continent: "AS" },
  
  // [DISABLED] Malaysia
  // { id: "my-kul", name: "Kuala Lumpur", country_code: "MY", country_name: "Malaysia", city: "Kuala Lumpur", latitude: 3.1390, longitude: 101.6869, timezone: "Asia/Kuala_Lumpur", continent: "AS" },
  // { id: "my-jhr", name: "Johor Bahru", country_code: "MY", country_name: "Malaysia", city: "Johor Bahru", latitude: 1.4927, longitude: 103.7414, timezone: "Asia/Kuala_Lumpur", continent: "AS" },
  // { id: "my-png", name: "Penang", country_code: "MY", country_name: "Malaysia", city: "George Town", latitude: 5.4141, longitude: 100.3288, timezone: "Asia/Kuala_Lumpur", continent: "AS" },
  // { id: "my-cyb", name: "Cyberjaya", country_code: "MY", country_name: "Malaysia", city: "Cyberjaya", latitude: 2.9213, longitude: 101.6559, timezone: "Asia/Kuala_Lumpur", continent: "AS" },
  
  // [DISABLED] Thailand
  // { id: "th-bkk", name: "Bangkok", country_code: "TH", country_name: "Thailand", city: "Bangkok", latitude: 13.7563, longitude: 100.5018, timezone: "Asia/Bangkok", continent: "AS" },
  // { id: "th-cnx", name: "Chiang Mai", country_code: "TH", country_name: "Thailand", city: "Chiang Mai", latitude: 18.7883, longitude: 98.9853, timezone: "Asia/Bangkok", continent: "AS" },
  
  // [DISABLED] Vietnam
  // { id: "vn-sgn", name: "Ho Chi Minh City", country_code: "VN", country_name: "Vietnam", city: "Ho Chi Minh City", latitude: 10.8231, longitude: 106.6297, timezone: "Asia/Ho_Chi_Minh", continent: "AS" },
  // { id: "vn-han", name: "Hanoi", country_code: "VN", country_name: "Vietnam", city: "Hanoi", latitude: 21.0278, longitude: 105.8342, timezone: "Asia/Ho_Chi_Minh", continent: "AS" },
  
  // [DISABLED] Philippines
  // { id: "ph-mnl", name: "Manila", country_code: "PH", country_name: "Philippines", city: "Manila", latitude: 14.5995, longitude: 120.9842, timezone: "Asia/Manila", continent: "AS" },
  // { id: "ph-ceb", name: "Cebu", country_code: "PH", country_name: "Philippines", city: "Cebu City", latitude: 10.3157, longitude: 123.8854, timezone: "Asia/Manila", continent: "AS" },
  
  // [DISABLED] Japan
  // { id: "jp-tyo", name: "Tokyo", country_code: "JP", country_name: "Japan", city: "Tokyo", latitude: 35.6762, longitude: 139.6503, timezone: "Asia/Tokyo", continent: "AS" },
  // { id: "jp-osa", name: "Osaka", country_code: "JP", country_name: "Japan", city: "Osaka", latitude: 34.6937, longitude: 135.5023, timezone: "Asia/Tokyo", continent: "AS" },
  // { id: "jp-fuk", name: "Fukuoka", country_code: "JP", country_name: "Japan", city: "Fukuoka", latitude: 33.5904, longitude: 130.4017, timezone: "Asia/Tokyo", continent: "AS" },
  // { id: "jp-ngo", name: "Nagoya", country_code: "JP", country_name: "Japan", city: "Nagoya", latitude: 35.1815, longitude: 136.9066, timezone: "Asia/Tokyo", continent: "AS" },
  
  // [DISABLED] South Korea
  // { id: "kr-sel", name: "Seoul", country_code: "KR", country_name: "South Korea", city: "Seoul", latitude: 37.5665, longitude: 126.9780, timezone: "Asia/Seoul", continent: "AS" },
  // { id: "kr-pus", name: "Busan", country_code: "KR", country_name: "South Korea", city: "Busan", latitude: 35.1796, longitude: 129.0756, timezone: "Asia/Seoul", continent: "AS" },
  
  // [DISABLED] China
  // { id: "cn-sha", name: "Shanghai", country_code: "CN", country_name: "China", city: "Shanghai", latitude: 31.2304, longitude: 121.4737, timezone: "Asia/Shanghai", continent: "AS" },
  // { id: "cn-bjs", name: "Beijing", country_code: "CN", country_name: "China", city: "Beijing", latitude: 39.9042, longitude: 116.4074, timezone: "Asia/Shanghai", continent: "AS" },
  // { id: "cn-szx", name: "Shenzhen", country_code: "CN", country_name: "China", city: "Shenzhen", latitude: 22.5431, longitude: 114.0579, timezone: "Asia/Shanghai", continent: "AS" },
  // { id: "cn-can", name: "Guangzhou", country_code: "CN", country_name: "China", city: "Guangzhou", latitude: 23.1291, longitude: 113.2644, timezone: "Asia/Shanghai", continent: "AS" },
  // { id: "cn-hkg", name: "Hong Kong", country_code: "HK", country_name: "Hong Kong", city: "Hong Kong", latitude: 22.3193, longitude: 114.1694, timezone: "Asia/Hong_Kong", continent: "AS" },
  
  // [DISABLED] Taiwan
  // { id: "tw-tpe", name: "Taipei", country_code: "TW", country_name: "Taiwan", city: "Taipei", latitude: 25.0330, longitude: 121.5654, timezone: "Asia/Taipei", continent: "AS" },
  
  // [DISABLED] India
  // { id: "in-bom", name: "Mumbai", country_code: "IN", country_name: "India", city: "Mumbai", latitude: 19.0760, longitude: 72.8777, timezone: "Asia/Kolkata", continent: "AS" },
  // { id: "in-del", name: "Delhi", country_code: "IN", country_name: "India", city: "New Delhi", latitude: 28.6139, longitude: 77.2090, timezone: "Asia/Kolkata", continent: "AS" },
  // { id: "in-blr", name: "Bangalore", country_code: "IN", country_name: "India", city: "Bangalore", latitude: 12.9716, longitude: 77.5946, timezone: "Asia/Kolkata", continent: "AS" },
  // { id: "in-maa", name: "Chennai", country_code: "IN", country_name: "India", city: "Chennai", latitude: 13.0827, longitude: 80.2707, timezone: "Asia/Kolkata", continent: "AS" },
  // { id: "in-hyd", name: "Hyderabad", country_code: "IN", country_name: "India", city: "Hyderabad", latitude: 17.3850, longitude: 78.4867, timezone: "Asia/Kolkata", continent: "AS" },
  // { id: "in-pnq", name: "Pune", country_code: "IN", country_name: "India", city: "Pune", latitude: 18.5204, longitude: 73.8567, timezone: "Asia/Kolkata", continent: "AS" },
  
  // [DISABLED] UAE
  // { id: "ae-dxb", name: "Dubai", country_code: "AE", country_name: "United Arab Emirates", city: "Dubai", latitude: 25.2048, longitude: 55.2708, timezone: "Asia/Dubai", continent: "AS" },
  // { id: "ae-auh", name: "Abu Dhabi", country_code: "AE", country_name: "United Arab Emirates", city: "Abu Dhabi", latitude: 24.4539, longitude: 54.3773, timezone: "Asia/Dubai", continent: "AS" },
  
  // [DISABLED] Saudi Arabia
  // { id: "sa-ruh", name: "Riyadh", country_code: "SA", country_name: "Saudi Arabia", city: "Riyadh", latitude: 24.7136, longitude: 46.6753, timezone: "Asia/Riyadh", continent: "AS" },
  // { id: "sa-jed", name: "Jeddah", country_code: "SA", country_name: "Saudi Arabia", city: "Jeddah", latitude: 21.4858, longitude: 39.1925, timezone: "Asia/Riyadh", continent: "AS" },
  
  // [DISABLED] Israel
  // { id: "il-tlv", name: "Tel Aviv", country_code: "IL", country_name: "Israel", city: "Tel Aviv", latitude: 32.0853, longitude: 34.7818, timezone: "Asia/Jerusalem", continent: "AS" },
  
  // [DISABLED] Qatar
  // { id: "qa-doh", name: "Doha", country_code: "QA", country_name: "Qatar", city: "Doha", latitude: 25.2854, longitude: 51.5310, timezone: "Asia/Qatar", continent: "AS" },
  
  // [DISABLED] Bahrain
  // { id: "bh-bah", name: "Manama", country_code: "BH", country_name: "Bahrain", city: "Manama", latitude: 26.2285, longitude: 50.5860, timezone: "Asia/Bahrain", continent: "AS" },
  
  // [DISABLED] ==================== EUROPE ====================
  // [DISABLED] United Kingdom
  // { id: "gb-lon", name: "London", country_code: "GB", country_name: "United Kingdom", city: "London", latitude: 51.5074, longitude: -0.1278, timezone: "Europe/London", continent: "EU" },
  // { id: "gb-man", name: "Manchester", country_code: "GB", country_name: "United Kingdom", city: "Manchester", latitude: 53.4808, longitude: -2.2426, timezone: "Europe/London", continent: "EU" },
  // { id: "gb-edi", name: "Edinburgh", country_code: "GB", country_name: "United Kingdom", city: "Edinburgh", latitude: 55.9533, longitude: -3.1883, timezone: "Europe/London", continent: "EU" },
  
  // [DISABLED] Germany
  // { id: "de-fra", name: "Frankfurt", country_code: "DE", country_name: "Germany", city: "Frankfurt", latitude: 50.1109, longitude: 8.6821, timezone: "Europe/Berlin", continent: "EU" },
  // { id: "de-ber", name: "Berlin", country_code: "DE", country_name: "Germany", city: "Berlin", latitude: 52.5200, longitude: 13.4050, timezone: "Europe/Berlin", continent: "EU" },
  // { id: "de-muc", name: "Munich", country_code: "DE", country_name: "Germany", city: "Munich", latitude: 48.1351, longitude: 11.5820, timezone: "Europe/Berlin", continent: "EU" },
  // { id: "de-dus", name: "Düsseldorf", country_code: "DE", country_name: "Germany", city: "Düsseldorf", latitude: 51.2277, longitude: 6.7735, timezone: "Europe/Berlin", continent: "EU" },
  
  // [DISABLED] France
  // { id: "fr-par", name: "Paris", country_code: "FR", country_name: "France", city: "Paris", latitude: 48.8566, longitude: 2.3522, timezone: "Europe/Paris", continent: "EU" },
  // { id: "fr-mrs", name: "Marseille", country_code: "FR", country_name: "France", city: "Marseille", latitude: 43.2965, longitude: 5.3698, timezone: "Europe/Paris", continent: "EU" },
  // { id: "fr-lys", name: "Lyon", country_code: "FR", country_name: "France", city: "Lyon", latitude: 45.7640, longitude: 4.8357, timezone: "Europe/Paris", continent: "EU" },
  
  // [DISABLED] Netherlands
  // { id: "nl-ams", name: "Amsterdam", country_code: "NL", country_name: "Netherlands", city: "Amsterdam", latitude: 52.3676, longitude: 4.9041, timezone: "Europe/Amsterdam", continent: "EU" },
  // { id: "nl-rtm", name: "Rotterdam", country_code: "NL", country_name: "Netherlands", city: "Rotterdam", latitude: 51.9244, longitude: 4.4777, timezone: "Europe/Amsterdam", continent: "EU" },
  
  // [DISABLED] Belgium
  // { id: "be-bru", name: "Brussels", country_code: "BE", country_name: "Belgium", city: "Brussels", latitude: 50.8503, longitude: 4.3517, timezone: "Europe/Brussels", continent: "EU" },
  
  // [DISABLED] Switzerland
  // { id: "ch-zrh", name: "Zurich", country_code: "CH", country_name: "Switzerland", city: "Zurich", latitude: 47.3769, longitude: 8.5417, timezone: "Europe/Zurich", continent: "EU" },
  // { id: "ch-gva", name: "Geneva", country_code: "CH", country_name: "Switzerland", city: "Geneva", latitude: 46.2044, longitude: 6.1432, timezone: "Europe/Zurich", continent: "EU" },
  
  // [DISABLED] Ireland
  // { id: "ie-dub", name: "Dublin", country_code: "IE", country_name: "Ireland", city: "Dublin", latitude: 53.3498, longitude: -6.2603, timezone: "Europe/Dublin", continent: "EU" },
  
  // [DISABLED] Spain
  // { id: "es-mad", name: "Madrid", country_code: "ES", country_name: "Spain", city: "Madrid", latitude: 40.4168, longitude: -3.7038, timezone: "Europe/Madrid", continent: "EU" },
  // { id: "es-bcn", name: "Barcelona", country_code: "ES", country_name: "Spain", city: "Barcelona", latitude: 41.3851, longitude: 2.1734, timezone: "Europe/Madrid", continent: "EU" },
  
  // [DISABLED] Italy
  // { id: "it-mil", name: "Milan", country_code: "IT", country_name: "Italy", city: "Milan", latitude: 45.4642, longitude: 9.1900, timezone: "Europe/Rome", continent: "EU" },
  // { id: "it-rom", name: "Rome", country_code: "IT", country_name: "Italy", city: "Rome", latitude: 41.9028, longitude: 12.4964, timezone: "Europe/Rome", continent: "EU" },
  
  // [DISABLED] Poland
  // { id: "pl-waw", name: "Warsaw", country_code: "PL", country_name: "Poland", city: "Warsaw", latitude: 52.2297, longitude: 21.0122, timezone: "Europe/Warsaw", continent: "EU" },
  // { id: "pl-krk", name: "Krakow", country_code: "PL", country_name: "Poland", city: "Krakow", latitude: 50.0647, longitude: 19.9450, timezone: "Europe/Warsaw", continent: "EU" },
  
  // [DISABLED] Sweden
  // { id: "se-sto", name: "Stockholm", country_code: "SE", country_name: "Sweden", city: "Stockholm", latitude: 59.3293, longitude: 18.0686, timezone: "Europe/Stockholm", continent: "EU" },
  
  // [DISABLED] Finland
  // { id: "fi-hel", name: "Helsinki", country_code: "FI", country_name: "Finland", city: "Helsinki", latitude: 60.1699, longitude: 24.9384, timezone: "Europe/Helsinki", continent: "EU" },
  
  // [DISABLED] Norway
  // { id: "no-osl", name: "Oslo", country_code: "NO", country_name: "Norway", city: "Oslo", latitude: 59.9139, longitude: 10.7522, timezone: "Europe/Oslo", continent: "EU" },
  
  // [DISABLED] Denmark
  // { id: "dk-cph", name: "Copenhagen", country_code: "DK", country_name: "Denmark", city: "Copenhagen", latitude: 55.6761, longitude: 12.5683, timezone: "Europe/Copenhagen", continent: "EU" },
  
  // [DISABLED] Austria
  // { id: "at-vie", name: "Vienna", country_code: "AT", country_name: "Austria", city: "Vienna", latitude: 48.2082, longitude: 16.3738, timezone: "Europe/Vienna", continent: "EU" },
  
  // [DISABLED] Czech Republic
  // { id: "cz-prg", name: "Prague", country_code: "CZ", country_name: "Czech Republic", city: "Prague", latitude: 50.0755, longitude: 14.4378, timezone: "Europe/Prague", continent: "EU" },
  
  // [DISABLED] Portugal
  // { id: "pt-lis", name: "Lisbon", country_code: "PT", country_name: "Portugal", city: "Lisbon", latitude: 38.7223, longitude: -9.1393, timezone: "Europe/Lisbon", continent: "EU" },
  
  // [DISABLED] Greece
  // { id: "gr-ath", name: "Athens", country_code: "GR", country_name: "Greece", city: "Athens", latitude: 37.9838, longitude: 23.7275, timezone: "Europe/Athens", continent: "EU" },
  
  // [DISABLED] Romania
  // { id: "ro-buh", name: "Bucharest", country_code: "RO", country_name: "Romania", city: "Bucharest", latitude: 44.4268, longitude: 26.1025, timezone: "Europe/Bucharest", continent: "EU" },
  
  // [DISABLED] Hungary
  // { id: "hu-bud", name: "Budapest", country_code: "HU", country_name: "Hungary", city: "Budapest", latitude: 47.4979, longitude: 19.0402, timezone: "Europe/Budapest", continent: "EU" },
  
  // [DISABLED] Russia
  // { id: "ru-mow", name: "Moscow", country_code: "RU", country_name: "Russia", city: "Moscow", latitude: 55.7558, longitude: 37.6173, timezone: "Europe/Moscow", continent: "EU" },
  // { id: "ru-led", name: "Saint Petersburg", country_code: "RU", country_name: "Russia", city: "Saint Petersburg", latitude: 59.9343, longitude: 30.3351, timezone: "Europe/Moscow", continent: "EU" },
  
  // [DISABLED] Ukraine
  // { id: "ua-kyv", name: "Kyiv", country_code: "UA", country_name: "Ukraine", city: "Kyiv", latitude: 50.4501, longitude: 30.5234, timezone: "Europe/Kyiv", continent: "EU" },
  
  // [DISABLED] ==================== NORTH AMERICA ====================
  // [DISABLED] United States - East
  // { id: "us-iad", name: "Virginia (N. Virginia)", country_code: "US", country_name: "United States", city: "Ashburn", latitude: 39.0438, longitude: -77.4874, timezone: "America/New_York", continent: "NA" },
  // { id: "us-nyc", name: "New York", country_code: "US", country_name: "United States", city: "New York", latitude: 40.7128, longitude: -74.0060, timezone: "America/New_York", continent: "NA" },
  // { id: "us-bos", name: "Boston", country_code: "US", country_name: "United States", city: "Boston", latitude: 42.3601, longitude: -71.0589, timezone: "America/New_York", continent: "NA" },
  // { id: "us-mia", name: "Miami", country_code: "US", country_name: "United States", city: "Miami", latitude: 25.7617, longitude: -80.1918, timezone: "America/New_York", continent: "NA" },
  // { id: "us-atl", name: "Atlanta", country_code: "US", country_name: "United States", city: "Atlanta", latitude: 33.7490, longitude: -84.3880, timezone: "America/New_York", continent: "NA" },
  
  // [DISABLED] United States - Central
  // { id: "us-ord", name: "Chicago", country_code: "US", country_name: "United States", city: "Chicago", latitude: 41.8781, longitude: -87.6298, timezone: "America/Chicago", continent: "NA" },
  // { id: "us-dfw", name: "Dallas", country_code: "US", country_name: "United States", city: "Dallas", latitude: 32.7767, longitude: -96.7970, timezone: "America/Chicago", continent: "NA" },
  // { id: "us-hou", name: "Houston", country_code: "US", country_name: "United States", city: "Houston", latitude: 29.7604, longitude: -95.3698, timezone: "America/Chicago", continent: "NA" },
  // { id: "us-den", name: "Denver", country_code: "US", country_name: "United States", city: "Denver", latitude: 39.7392, longitude: -104.9903, timezone: "America/Denver", continent: "NA" },
  
  // [DISABLED] United States - West
  // { id: "us-sfo", name: "San Francisco", country_code: "US", country_name: "United States", city: "San Francisco", latitude: 37.7749, longitude: -122.4194, timezone: "America/Los_Angeles", continent: "NA" },
  // { id: "us-lax", name: "Los Angeles", country_code: "US", country_name: "United States", city: "Los Angeles", latitude: 34.0522, longitude: -118.2437, timezone: "America/Los_Angeles", continent: "NA" },
  // { id: "us-sea", name: "Seattle", country_code: "US", country_name: "United States", city: "Seattle", latitude: 47.6062, longitude: -122.3321, timezone: "America/Los_Angeles", continent: "NA" },
  // { id: "us-phx", name: "Phoenix", country_code: "US", country_name: "United States", city: "Phoenix", latitude: 33.4484, longitude: -112.0740, timezone: "America/Phoenix", continent: "NA" },
  // { id: "us-las", name: "Las Vegas", country_code: "US", country_name: "United States", city: "Las Vegas", latitude: 36.1699, longitude: -115.1398, timezone: "America/Los_Angeles", continent: "NA" },
  // { id: "us-pdx", name: "Portland", country_code: "US", country_name: "United States", city: "Portland", latitude: 45.5152, longitude: -122.6784, timezone: "America/Los_Angeles", continent: "NA" },
  // { id: "us-slc", name: "Salt Lake City", country_code: "US", country_name: "United States", city: "Salt Lake City", latitude: 40.7608, longitude: -111.8910, timezone: "America/Denver", continent: "NA" },
  
  // [DISABLED] Canada
  // { id: "ca-yyz", name: "Toronto", country_code: "CA", country_name: "Canada", city: "Toronto", latitude: 43.6532, longitude: -79.3832, timezone: "America/Toronto", continent: "NA" },
  // { id: "ca-yul", name: "Montreal", country_code: "CA", country_name: "Canada", city: "Montreal", latitude: 45.5017, longitude: -73.5673, timezone: "America/Montreal", continent: "NA" },
  // { id: "ca-yvr", name: "Vancouver", country_code: "CA", country_name: "Canada", city: "Vancouver", latitude: 49.2827, longitude: -123.1207, timezone: "America/Vancouver", continent: "NA" },
  // { id: "ca-yyc", name: "Calgary", country_code: "CA", country_name: "Canada", city: "Calgary", latitude: 51.0447, longitude: -114.0719, timezone: "America/Edmonton", continent: "NA" },
  
  // [DISABLED] Mexico
  // { id: "mx-mex", name: "Mexico City", country_code: "MX", country_name: "Mexico", city: "Mexico City", latitude: 19.4326, longitude: -99.1332, timezone: "America/Mexico_City", continent: "NA" },
  // { id: "mx-qro", name: "Querétaro", country_code: "MX", country_name: "Mexico", city: "Querétaro", latitude: 20.5888, longitude: -100.3899, timezone: "America/Mexico_City", continent: "NA" },
  // { id: "mx-gdl", name: "Guadalajara", country_code: "MX", country_name: "Mexico", city: "Guadalajara", latitude: 20.6597, longitude: -103.3496, timezone: "America/Mexico_City", continent: "NA" },
  
  // [DISABLED] ==================== SOUTH AMERICA ====================
  // [DISABLED] Brazil
  // { id: "br-gru", name: "São Paulo", country_code: "BR", country_name: "Brazil", city: "São Paulo", latitude: -23.5505, longitude: -46.6333, timezone: "America/Sao_Paulo", continent: "SA" },
  // { id: "br-gig", name: "Rio de Janeiro", country_code: "BR", country_name: "Brazil", city: "Rio de Janeiro", latitude: -22.9068, longitude: -43.1729, timezone: "America/Sao_Paulo", continent: "SA" },
  // { id: "br-for", name: "Fortaleza", country_code: "BR", country_name: "Brazil", city: "Fortaleza", latitude: -3.7319, longitude: -38.5267, timezone: "America/Fortaleza", continent: "SA" },
  
  // [DISABLED] Argentina
  // { id: "ar-eze", name: "Buenos Aires", country_code: "AR", country_name: "Argentina", city: "Buenos Aires", latitude: -34.6037, longitude: -58.3816, timezone: "America/Argentina/Buenos_Aires", continent: "SA" },
  
  // [DISABLED] Chile
  // { id: "cl-scl", name: "Santiago", country_code: "CL", country_name: "Chile", city: "Santiago", latitude: -33.4489, longitude: -70.6693, timezone: "America/Santiago", continent: "SA" },
  
  // [DISABLED] Colombia
  // { id: "co-bog", name: "Bogotá", country_code: "CO", country_name: "Colombia", city: "Bogotá", latitude: 4.7110, longitude: -74.0721, timezone: "America/Bogota", continent: "SA" },
  // { id: "co-med", name: "Medellín", country_code: "CO", country_name: "Colombia", city: "Medellín", latitude: 6.2442, longitude: -75.5812, timezone: "America/Bogota", continent: "SA" },
  
  // [DISABLED] Peru
  // { id: "pe-lim", name: "Lima", country_code: "PE", country_name: "Peru", city: "Lima", latitude: -12.0464, longitude: -77.0428, timezone: "America/Lima", continent: "SA" },
  
  // [DISABLED] Ecuador
  // { id: "ec-uio", name: "Quito", country_code: "EC", country_name: "Ecuador", city: "Quito", latitude: -0.1807, longitude: -78.4678, timezone: "America/Guayaquil", continent: "SA" },
  
  // [DISABLED] ==================== OCEANIA ====================
  // [DISABLED] Australia
  // { id: "au-syd", name: "Sydney", country_code: "AU", country_name: "Australia", city: "Sydney", latitude: -33.8688, longitude: 151.2093, timezone: "Australia/Sydney", continent: "OC" },
  // { id: "au-mel", name: "Melbourne", country_code: "AU", country_name: "Australia", city: "Melbourne", latitude: -37.8136, longitude: 144.9631, timezone: "Australia/Melbourne", continent: "OC" },
  // { id: "au-bne", name: "Brisbane", country_code: "AU", country_name: "Australia", city: "Brisbane", latitude: -27.4698, longitude: 153.0251, timezone: "Australia/Brisbane", continent: "OC" },
  // { id: "au-per", name: "Perth", country_code: "AU", country_name: "Australia", city: "Perth", latitude: -31.9505, longitude: 115.8605, timezone: "Australia/Perth", continent: "OC" },
  // { id: "au-adl", name: "Adelaide", country_code: "AU", country_name: "Australia", city: "Adelaide", latitude: -34.9285, longitude: 138.6007, timezone: "Australia/Adelaide", continent: "OC" },
  
  // [DISABLED] New Zealand
  // { id: "nz-akl", name: "Auckland", country_code: "NZ", country_name: "New Zealand", city: "Auckland", latitude: -36.8509, longitude: 174.7645, timezone: "Pacific/Auckland", continent: "OC" },
  // { id: "nz-wlg", name: "Wellington", country_code: "NZ", country_name: "New Zealand", city: "Wellington", latitude: -41.2865, longitude: 174.7762, timezone: "Pacific/Auckland", continent: "OC" },
  
  // [DISABLED] ==================== AFRICA ====================
  // [DISABLED] South Africa
  // { id: "za-jnb", name: "Johannesburg", country_code: "ZA", country_name: "South Africa", city: "Johannesburg", latitude: -26.2041, longitude: 28.0473, timezone: "Africa/Johannesburg", continent: "AF" },
  // { id: "za-cpt", name: "Cape Town", country_code: "ZA", country_name: "South Africa", city: "Cape Town", latitude: -33.9249, longitude: 18.4241, timezone: "Africa/Johannesburg", continent: "AF" },
  
  // [DISABLED] Egypt
  // { id: "eg-cai", name: "Cairo", country_code: "EG", country_name: "Egypt", city: "Cairo", latitude: 30.0444, longitude: 31.2357, timezone: "Africa/Cairo", continent: "AF" },
  
  // [DISABLED] Nigeria
  // { id: "ng-los", name: "Lagos", country_code: "NG", country_name: "Nigeria", city: "Lagos", latitude: 6.5244, longitude: 3.3792, timezone: "Africa/Lagos", continent: "AF" },
  
  // [DISABLED] Kenya
  // { id: "ke-nbo", name: "Nairobi", country_code: "KE", country_name: "Kenya", city: "Nairobi", latitude: -1.2921, longitude: 36.8219, timezone: "Africa/Nairobi", continent: "AF" },
  
  // [DISABLED] Morocco
  // { id: "ma-cas", name: "Casablanca", country_code: "MA", country_name: "Morocco", city: "Casablanca", latitude: 33.5731, longitude: -7.5898, timezone: "Africa/Casablanca", continent: "AF" },
  
  // [DISABLED] Tunisia
  // { id: "tn-tun", name: "Tunis", country_code: "TN", country_name: "Tunisia", city: "Tunis", latitude: 36.8065, longitude: 10.1815, timezone: "Africa/Tunis", continent: "AF" },
  
  // [DISABLED] Ghana
  // { id: "gh-acc", name: "Accra", country_code: "GH", country_name: "Ghana", city: "Accra", latitude: 5.6037, longitude: -0.1870, timezone: "Africa/Accra", continent: "AF" },
];

// Helper to get all unique countries
export function getCountries(): Array<{ code: string; name: string; continent: string }> {
  const countryMap = new Map<string, { code: string; name: string; continent: string }>();
  
  REGIONS_DATA.forEach((region) => {
    if (!countryMap.has(region.country_code)) {
      countryMap.set(region.country_code, {
        code: region.country_code,
        name: region.country_name,
        continent: region.continent,
      });
    }
  });
  
  return Array.from(countryMap.values()).sort((a, b) => a.name.localeCompare(b.name));
}

// Helper to get regions by country
export function getRegionsByCountry(countryCode: string): Region[] {
  return REGIONS_DATA.filter((r) => r.country_code === countryCode);
}

// Helper to get region by ID
export function getRegionById(regionId: string): Region | undefined {
  return REGIONS_DATA.find((r) => r.id === regionId);
}

// Helper to search regions by name or city
export function searchRegions(query: string): Region[] {
  const lowerQuery = query.toLowerCase();
  return REGIONS_DATA.filter(
    (r) =>
      r.name.toLowerCase().includes(lowerQuery) ||
      r.city.toLowerCase().includes(lowerQuery) ||
      r.country_name.toLowerCase().includes(lowerQuery)
  );
}

// Helper to get countries grouped by continent
export function getCountriesByContinent(): Record<string, Array<{ code: string; name: string }>> {
  const result: Record<string, Array<{ code: string; name: string }>> = {};
  
  Object.keys(CONTINENTS).forEach((continent) => {
    result[continent] = [];
  });
  
  getCountries().forEach((country) => {
    if (result[country.continent]) {
      result[country.continent].push({ code: country.code, name: country.name });
    }
  });
  
  return result;
}

// Default coordinates for a custom location (center of Indonesia)
export const DEFAULT_COORDINATES = {
  latitude: -2.5,
  longitude: 118.0,
};

// Map bounds for initial view - strictly Indonesia
// Bounds: North (6°N Sabang), South (-11°S Rote Island), West (95°E Sabang), East (141°E Papua)
export const INDONESIA_BOUNDS = {
  center: { lat: -2.5, lng: 118.0 } as const,  // Center of Indonesia
  zoom: 5,  // Zoom level for Indonesia view
  minZoom: 4,  // Prevent zooming out too far
  maxZoom: 18,
  // Strict bounds to prevent panning outside Indonesia
  maxBounds: {
    north: 8.0,    // Slightly north of Sabang
    south: -12.0,  // Slightly south of Rote Island
    west: 94.0,    // Slightly west of Sabang
    east: 142.0,   // Slightly east of Papua
  },
};

// Legacy alias for backward compatibility
export const WORLD_BOUNDS = INDONESIA_BOUNDS;
