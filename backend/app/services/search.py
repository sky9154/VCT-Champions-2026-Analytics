from typing import Any

from pymongo.database import Database

from app.assets import public_team_logo_url
from app.api.responses import public_identifier
from app.config import get_settings
from app.services.common import find_tournament_player_ids, find_tournament_team_ids


def _search_values(document: dict[str, Any], fields: tuple[str,
                                                           ...]) -> list[str]:
  return [
      value.strip() for field in fields
      if isinstance((value := document.get(field)), str) and value.strip()
  ]


def _matches_query(values: list[str], query: str) -> bool:
  return any(query in value.casefold() for value in values)


def _search_sort_key(document: dict[str, Any], query: str, fields: tuple[str,
                                                                         ...],
                     label_field: str) -> tuple[Any, ...]:
  values = _search_values(document, fields)
  is_prefix = any(value.casefold().startswith(query) for value in values)
  label = document.get(label_field)
  slug = document.get("slug")

  return (0 if is_prefix else 1,
          label.casefold() if isinstance(label, str) else "",
          slug.casefold() if isinstance(slug, str) else "",
          slug if isinstance(slug, str) else "")


def search_event(database: Database, tournament_id: Any | None,
                 query: str) -> tuple[dict[str, Any], int]:
  if tournament_id is None or not query:
    return {"teams": [], "players": []}, 0

  team_ids = find_tournament_team_ids(database, tournament_id)
  team_documents = list(database["teams"].find(
      {"_id": {
          "$in": list(team_ids)
      }}, {
          "slug": 1,
          "name": 1,
          "shortName": 1,
          "logoUrl": 1
      })) if team_ids else []
  team_fields = ("name", "shortName", "slug")
  matching_teams = [
      team for team in team_documents
      if _matches_query(_search_values(team, team_fields), query)
  ]
  matching_teams.sort(
      key=lambda team: _search_sort_key(team, query, team_fields, "name"))

  player_ids = find_tournament_player_ids(database, tournament_id)
  player_documents = list(database["players"].find(
      {"_id": {
          "$in": list(player_ids)
      }}, {
          "slug": 1,
          "handle": 1,
          "realName": 1,
          "currentTeamId": 1
      })) if player_ids else []
  player_fields = ("handle", "slug", "realName")
  matching_players = [
      player for player in player_documents
      if _matches_query(_search_values(player, player_fields), query)
  ]
  matching_players.sort(key=lambda player: _search_sort_key(
      player, query, player_fields, "handle"))

  selected_players = matching_players[:5]
  current_team_ids = {
      player.get("currentTeamId")
      for player in selected_players if player.get("currentTeamId") is not None
  }
  current_teams_by_id = {
      team["_id"]: team
      for team in database["teams"].find(
          {"_id": {
              "$in": list(current_team_ids)
          }}, {
              "slug": 1,
              "name": 1,
              "shortName": 1
          })
  } if current_team_ids else {}

  response_players = []

  for player in selected_players:
    current_team = current_teams_by_id.get(player.get("currentTeamId"))
    response_players.append({
        "slug": public_identifier(player.get("slug")),
        "handle": player.get("handle"),
        "currentTeam": {
            "slug": public_identifier(current_team.get("slug")),
            "name": current_team.get("name"),
            "shortName": current_team.get("shortName")
        } if current_team else None
    })

  response_teams = [{
      "slug":
      public_identifier(team.get("slug")),
      "name":
      team.get("name"),
      "shortName":
      team.get("shortName"),
      "logoUrl":
      public_team_logo_url(get_settings(), public_identifier(team.get("slug")))
  } for team in matching_teams[:5]]
  total = len(matching_teams) + len(matching_players)

  return {"teams": response_teams, "players": response_players}, total
