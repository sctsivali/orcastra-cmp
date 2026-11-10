import type { GeoLocation, RegionSummary } from "@/types/api";
import type { Region } from "./catalog";

/**
 * How a location reads in text, whichever shape it arrives in: a catalogue entry in the
 * pickers, a stored location on a cluster, or a region on the map.
 *
 * A region outside Indonesia stands for the whole country, because the site of a cluster abroad
 * is rarely known when it is registered. Nothing here reads `city` for one, so a stored location
 * whose `city` names the capital never shows it.
 */
type PlaceFields = "city" | "country_code" | "country_name" | "province";
export type Place =
  | Pick<Region, "id" | "name" | PlaceFields>
  | Pick<GeoLocation, "region_id" | "region_name" | PlaceFields>
  | Pick<RegionSummary, "region_id" | "region_name" | PlaceFields>;

function idOf(place: Place): string {
  return "region_id" in place ? place.region_id : place.id;
}

function nameOf(place: Place): string {
  return "region_name" in place ? place.region_name : place.name;
}

// Decided by the id, which the server rewrites for every catalogue region, rather than by
// "outside Indonesia": an API client's own location abroad (us-east-dc1 in Ashburn) carries a
// real site and stays a pin.
export function isWholeCountry(place: Place): boolean {
  const code = place.country_code;
  return Boolean(code) && idOf(place) === `c-${code.toLowerCase()}`;
}

/** The line under a region's name: "Cibinong, Jawa Barat", or "Whole country". */
export function placeDetail(place: Place): string {
  if (isWholeCountry(place)) return "Whole country";
  return `${place.city}, ${place.province ?? place.country_name}`;
}

/** A region named in a sentence: "Bogor (Regency), Jawa Barat", or "Japan". */
export function placeName(place: Place): string {
  if (isWholeCountry(place)) return place.country_name;
  return `${nameOf(place)}, ${place.province ?? place.country_name}`;
}

/** The one word a tight label has room for: the seat town, or the country. */
export function placeShort(place: Place): string {
  return isWholeCountry(place) ? place.country_name : place.city;
}
