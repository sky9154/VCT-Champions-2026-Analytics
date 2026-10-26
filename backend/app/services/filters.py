from typing import Any

from pymongo.database import Database

from app.api.responses import public_identifier
from app.services.common import distinct_nonempty_strings, find_tournament_player_ids, referenced_team_ids

SIDES = ["overall", "attack", "defense"]


def empty_filters() -> dict[str, Any]:
  return {
      "stages": [],
      "teams": [],
      "roles": [],
      "agents": [],
      "maps": [],
      "sides": SIDES
  }


def get_event_filters(database: Database,
                      tournament_id: Any | None) -> dict[str, Any]:
  if tournament_id is None:
    return empty_filters()

  matches = list(database["matches"].find({"tournamentId": tournament_id}, {
      "stage": 1,
      "teams.teamAId": 1,
      "teams.teamBId": 1
  }))
  stages = distinct_nonempty_strings([match.get("stage") for match in matches])
  team_ids = {
      team_id
      for match in matches
      for team_id in referenced_team_ids(match)
  }
  teams = list(database["teams"].find({"_id": {
      "$in": list(team_ids)
  }}, {
      "slug": 1,
      "name": 1,
      "shortName": 1
  })) if team_ids else []

  teams.sort(
      key=lambda team: ((team.get("name") or "").casefold() if isinstance(
          team.get("name"), str) else "", (team.get("slug") or "").casefold()
                        if isinstance(team.get("slug"), str) else ""))

  player_ids = find_tournament_player_ids(database, tournament_id)
  players = list(database["players"].find({"_id": {
      "$in": list(player_ids)
  }}, {"role": 1})) if player_ids else []
  roles = distinct_nonempty_strings([player.get("role") for player in players])
  agents = distinct_nonempty_strings(database["player_map_stats"].distinct(
      "agent", {"tournamentId": tournament_id}))
  maps = distinct_nonempty_strings(database["match_maps"].distinct(
      "mapName", {"tournamentId": tournament_id}))

  return {
      "stages":
      stages,
      "teams": [{
          "slug": public_identifier(team.get("slug")),
          "name": team.get("name"),
          "shortName": team.get("shortName")
      } for team in teams],
      "roles":
      roles,
      "agents":
      agents,
      "maps":
      maps,
      "sides":
      SIDES
  }
