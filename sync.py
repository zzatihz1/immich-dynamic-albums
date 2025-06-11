from argparse import ArgumentParser, ArgumentTypeError
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Union

import itertools
import json
import os
import time
import uuid

import jsonschema
import requests
import schedule

from semver.version import Version


class Immich:
    # ... (Immich class remains the same as your latest version with pagination) ...
    def __init__(self, immich_url: str, api_key: str) -> None:
        self.immich_url = immich_url.rstrip("/")
        self.api_key = api_key

    def whoami(self):
        return self._get("/api/users/me")

    def version(self):
        return self._get("/api/server/version")

    def get_people(self):
        return self._get("/api/people?size=1000&withHidden=false")

    def get_tags(self):
        return self._get("/api/tags")

    def get_albums(self):
        return self._get("/api/albums?shared=false")

    def get_album(self, album_id: str, with_assets: bool = False):
        return self._get(f"/api/albums/{album_id}?withoutAssets={json.dumps(not with_assets)}")

    def create_album(self, name: str, description: str = None):
        album_params = {
            "albumName": name,
        }

        if description:
            album_params["description"] = description

        return self._post("/api/albums", album_params)

    def delete_assets_from_album(self, album_id: str, assets_ids: List[str]):
        delete_params = {"ids": assets_ids}

        return self._delete(f"/api/albums/{album_id}/assets", delete_params)

    def add_assets_to_album(self, album_id: str, assets_ids: List[str]):
        add_params = {"ids": assets_ids}

        return self._put(f"/api/albums/{album_id}/assets", add_params)

    def search(
        self,
        country: str = None,
        state: str = None,
        city: str = None,
        before: datetime = None,
        after: datetime = None,
        favorite: bool = None,
        person_ids: List[str] = None,
        tag_ids: List[str] = None,
        type: Union[str, List[str]] = None,
    ):
        base_search_params = {
            "isVisible": True,
            "withExif": True,
        }

        if country:
            base_search_params["country"] = country
        if state:
            base_search_params["state"] = state
        if city:
            base_search_params["city"] = city
        if before:
            base_search_params["takenBefore"] = before.isoformat()
        if after:
            base_search_params["takenAfter"] = after.isoformat()
        if favorite is not None:
            base_search_params["isFavorite"] = favorite
        if person_ids:
            base_search_params["personIds"] = person_ids
        if tag_ids:
            base_search_params["tagIds"] = tag_ids
        if type:
            base_search_params["type"] = type

        all_assets_items = []
        current_page = 1
        page_size = 250

        while True:
            paginated_search_params = {
                **base_search_params,
                "size": page_size,
                "page": current_page,
            }

            response_json = self._post("/api/search/metadata", paginated_search_params)
            
            items_on_this_page = []
            if response_json and \
               isinstance(response_json.get("assets"), dict) and \
               isinstance(response_json["assets"].get("items"), list):
                items_on_this_page = response_json["assets"]["items"]
            else:
                print(f"Warning: Unexpected response structure or empty assets object during search pagination (page {current_page}): {response_json}")
                break 

            if not items_on_this_page:
                break

            all_assets_items.extend(items_on_this_page)

            if len(items_on_this_page) < page_size:
                break
            
            current_page += 1

        return {
            "assets": {
                "items": all_assets_items
            }
        }

    def _get(self, path, payload = {}):
        return self._api("GET", path, payload)

    def _put(self, path, payload):
        return self._api("PUT", path, json.dumps(payload))

    def _post(self, path, payload):
        return self._api("POST", path, json.dumps(payload))

    def _delete(self, path, payload):
        return self._api("DELETE", path, json.dumps(payload))

    def _api(self, verb: str, path: str, payload: Any):
        url = f"{self.immich_url}/{path.lstrip('/')}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": self.api_key,
        }

        response = requests.request(verb, url, headers=headers, data=payload, timeout=60)
        if response.status_code >= 400:
            print(url)
            print(response.text)

        response.raise_for_status()

        return response.json()


def create_album_if_not_exists(immich: Immich, album_name: str) -> str:
    # ... (This function remains the same) ...
    albums = immich.get_albums()
    album_names = {album["albumName"]: album for album in albums}

    if album_name in album_names.keys():
        return album_names[album_name]

    album = immich.create_album(album_name)

    return album


def read_json(config_path: Union[Path, str]) -> Any:
    # ... (This function remains the same) ...
    with open(config_path) as f:
        return json.load(f)


def config_query_to_search_queries(query: Dict, people_mapping: Dict[str, str], tag_mapping: Dict[str, str]) -> List[Dict]:
    # query_copy = query.copy() # Work on a copy if manipulating `query` directly and it's reused

    # Handle 'people' (assumed AND logic for all listed people)
    and_person_params = {}
    if "people" in query:
        people_values = query.pop("people") # Pop to avoid passing it in `**query` later
        if not isinstance(people_values, list):
            people_values = [people_values]
        
        resolved_and_ids = []
        unresolved_and_names = []
        for p_val in people_values:
            p_id = p_val if is_valid_uuid(p_val) else people_mapping.get(p_val)
            if not p_id:
                unresolved_and_names.append(p_val)
            else:
                resolved_and_ids.append(p_id)
        
        if unresolved_and_names:
            raise ValueError(f"The following names in 'people' do not exist in Immich: {unresolved_and_names}")
        
        if resolved_and_ids:
            and_person_params = {"person_ids": resolved_and_ids}

    # Handle 'any_people' (OR logic for listed people)
    # This will generate a list of parameter dicts, e.g., [{"person_ids": [id1]}, {"person_ids": [id2]}]
    # If 'any_people' is not used or resolves to nothing, it's effectively one empty parameter set [{}]
    or_people_param_iterations = [{}] 
    uses_any_people_logic = False
    if "any_people" in query:
        if and_person_params: # Check for conflict with 'people'
            raise ValueError("Cannot use 'people' (AND logic) and 'any_people' (OR logic) simultaneously in the same query block.")
            
        any_people_values = query.pop("any_people") # Pop it
        # Schema (minItems: 1) should ensure any_people_values is a non-empty list if key is present.
        # If it could be a single string, add: if not isinstance(any_people_values, list): any_people_values = [any_people_values]

        resolved_or_ids = []
        unresolved_or_names = []
        for p_val in any_people_values:
            p_id = p_val if is_valid_uuid(p_val) else people_mapping.get(p_val)
            if not p_id:
                unresolved_or_names.append(p_val)
            else:
                resolved_or_ids.append(p_id)

        if unresolved_or_names:
            raise ValueError(f"The following names in 'any_people' do not exist in Immich: {unresolved_or_names}")

        if resolved_or_ids: # If list was not empty and people were resolved
            or_people_param_iterations = [{"person_ids": [pid]} for pid in resolved_or_ids]
            uses_any_people_logic = True
        # If resolved_or_ids is empty (e.g., an empty list was somehow passed despite schema),
        # or_people_param_iterations remains [{}] and uses_any_people_logic is False.

    # Handle 'tags' (similar to 'people', assumed AND logic) - existing logic
    if "tags" in query:
        tags = query.pop("tags")
        if not isinstance(tags, list):
            tags = [tags]
        tag_ids = [tag if is_valid_uuid(tag) else tag_mapping.get(tag) for tag in tags]
        if None in tag_ids:
            invalid_tags = [tags[idx] for idx, value_or_id in enumerate(tag_ids) if not value_or_id]
            raise ValueError(f"The following tags do not exist in Immich: {invalid_tags}")
        if tag_ids: # Only add if there are resolved tag IDs
            query["tag_ids"] = tag_ids


    # Prepare other iterables for the product
    query_countries = query.pop("country", [None])
    if isinstance(query_countries, str):
        query_countries = [query_countries]
    elif not isinstance(query_countries, list): # Should be caught by schema, but good check
        raise ValueError("'country' has to be either a string or a list of strings")

    query_timespans_config = query.pop("timespan", [])
    if isinstance(query_timespans_config, dict):
        query_timespans_config = [query_timespans_config]
    elif not isinstance(query_timespans_config, list): # Should be caught by schema
        raise ValueError("'timespan' has to be either a dict or a list of dicts")
    
    processed_timespans = [
        {
            "before": datetime.strptime(q["end"], "%Y-%m-%d") + timedelta(hours=24),
            "after": datetime.strptime(q["start"], "%Y-%m-%d")
        }
        for q in query_timespans_config
    ]
    if not processed_timespans:
        processed_timespans.append({"before": None, "after": None}) # Default timespan if none provided

    # 'query' dict now contains remaining filters (type, favorite, state, city etc.)
    # It no longer contains 'people', 'any_people', 'country', 'timespan', 'tags' (if they were processed into specific keys)

    for country_val, timespan_param, or_person_param in itertools.product(
        query_countries,
        processed_timespans,
        or_people_param_iterations # This list drives the OR logic iterations for people
    ):
        subquery = {
            "country": country_val, # country_val can be None
            **timespan_param,       # e.g., {"before": ..., "after": ...}
            **query                 # Spread remaining original query items (type, favorite, city, tag_ids etc.)
        }

        if uses_any_people_logic:
            # 'any_people' was specified and resulted in OR branches.
            # or_person_param is like {"person_ids": [one_id_from_or_list]}
            subquery.update(or_person_param)
        elif and_person_params:
            # 'any_people' was NOT used (or yielded no valid people/branches),
            # so apply 'people' (AND logic) if it was specified.
            subquery.update(and_person_params)
        
        # else: no specific person filter from 'people' or 'any_people' was applied.

        # Remove 'country' from subquery if it's None, as Immich.search takes None directly for optional args
        if subquery["country"] is None:
            del subquery["country"]
        # Similarly for 'before'/'after' if they are None (Immich.search handles None for these)
        if timespan_param["before"] is None and "before" in subquery: # only delete if it was explicitly None from default
             del subquery["before"]
        if timespan_param["after"] is None and "after" in subquery: # only delete if it was explicitly None from default
             del subquery["after"]

        yield subquery


def is_valid_uuid(value: str) -> bool:
    # ... (This function remains the same) ...
    try:
        return bool(uuid.UUID(value))
    except ValueError:
        return False


def valid_input_file_arg(arg: Union[Path, str]) -> Path:
    # ... (This function remains the same) ...
    path = Path(arg).resolve()

    if not path.exists():
        raise ArgumentTypeError(f"Path does not exist: {arg}")
    if not path.is_file():
        raise ArgumentTypeError(f"Path is not a file: {arg}")

    return path


def sync_albums(args):
    # ... (sync_albums function remains largely the same) ...
    # It will now potentially receive more queries from config_query_to_search_queries
    # if 'any_people' is used, but its aggregation logic using set() for asset IDs
    # will correctly handle duplicates arising from the OR logic.

    configs = read_json(args.config_file)
    schema_path = Path(__file__).parent / "schema.json"
    if not schema_path.exists():
        print(f"Warning: schema.json not found at {schema_path}. Validation might fail or use an incorrect schema.")
    
    schema = read_json(schema_path)
    jsonschema.validate(instance=configs, schema=schema)

    immich = Immich(args.immich_url, args.immich_api_key)

    immich_version_data = immich.version()
    immich_version = Version(**immich_version_data)
    print(f"Immich version: {immich_version}")

    # Example min_supported_version, adjust if new features rely on newer Immich
    min_supported_version = Version(1, 100, 0) # Adjusted for example
    assert immich_version >= min_supported_version, f"Minimum supported version is {min_supported_version}"

    people_data = immich.get_people()
    people_name_to_id = {p["name"]: p["id"] for p in people_data.get("people", [])}


    tags_data = immich.get_tags()
    tag_value_to_id = {t["value"]: t["id"] for t in tags_data}


    for config in configs:
        album_name = config["name"]
        print(f"Processing album {album_name}")

        # config_query_to_search_queries now handles 'any_people'
        search_queries_params = list(
            config_query_to_search_queries(config["query"], people_mapping=people_name_to_id, tag_mapping=tag_value_to_id)
        )
        print(f"Album search queries: {search_queries_params}")

        search_responses = [immich.search(**params) for params in search_queries_params]
        
        search_items_per_query = [response["assets"]["items"] for response in search_responses]
        all_query_items = list(itertools.chain(*search_items_per_query))

        search_assets_ids = {asset["id"] for asset in all_query_items} # Use set for auto-deduplication

        album_without_assets = create_album_if_not_exists(immich, album_name)
        album = immich.get_album(album_without_assets["id"], with_assets=True)
        album_assets_ids = {asset["id"] for asset in album.get("assets", [])}

        album_missing_assets_ids = list(search_assets_ids - album_assets_ids)
        album_extra_assets_ids = list(album_assets_ids - search_assets_ids)

        print(f"Found {len(search_assets_ids)} unique assets for the album criteria.")
        print(f"Missing assets to add: {len(album_missing_assets_ids)}")
        print(f"Extra assets to remove: {len(album_extra_assets_ids)}")

        if album_extra_assets_ids:
            immich.delete_assets_from_album(album["id"], album_extra_assets_ids)

        if album_missing_assets_ids:
            immich.add_assets_to_album(album["id"], album_missing_assets_ids)

        print("Done")


def parse_args():
    # ... (This function remains the same) ...
    parser = ArgumentParser(description="Update dynamic albums")
    parser.add_argument(
        "--immich-url",
        default=os.environ.get("IMMICH_URL", "http://localhost:2283"),
    )
    parser.add_argument(
        "--immich-api-key",
        default=os.environ.get("IMMICH_API_KEY"),
    )
    parser.add_argument(
        "--config-file",
        type=valid_input_file_arg,
        default=os.environ.get("CONFIG_FILE"),
    )
    parser.add_argument(
        "--schedule-interval",
        type=int,
        default=os.environ.get("SCHEDULE_INTERVAL", 0),
        help="Schedule interval in minutes",
    )
    return parser.parse_args()


def main():
    # ... (This function remains the same) ...
    args = parse_args()

    assert args.immich_api_key, "immich-api-key is required"
    assert args.config_file, "config-file is required"

    sync_albums(args)

    interval_in_minutes = args.schedule_interval
    if interval_in_minutes > 0:
        print(f"Scheduling the job to run every {interval_in_minutes} minutes")
        schedule.every(interval_in_minutes).minutes.do(sync_albums, args=args)

        while True:
            schedule.run_pending()
            time.sleep(60)


if __name__ == "__main__":
    main()
