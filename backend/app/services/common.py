from typing import Any

from pymongo.database import Database


def find_tournament_id(database: Database, tournament_slug: str) -> Any | None:
  tournament = database["tournaments"].find_one({"slug": tournament_slug},
                                                {"_id": 1})

  return tournament.get("_id") if tournament else None


def referenced_team_ids(match: dict[str, Any]) -> set[Any]:
  teams = match.get("teams")

  if not isinstance(teams, dict):
    return set()

  return {
      team_id
      for team_id in (teams.get("teamAId"), teams.get("teamBId"))
      if team_id is not None
  }


def find_tournament_team_ids(database: Database,
                             tournament_id: Any) -> set[Any]:
  return {
      team_id
      for match in database["matches"].find({"tournamentId": tournament_id}, {
          "teams.teamAId": 1,
          "teams.teamBId": 1
      })
      for team_id in referenced_team_ids(match)
  }


def find_tournament_player_ids(database: Database,
                               tournament_id: Any) -> set[Any]:
  return {
      player_id
      for player_id in database["player_map_stats"].distinct(
          "playerId", {"tournamentId": tournament_id}) if player_id is not None
  }


def distinct_nonempty_strings(values: list[Any]) -> list[str]:
  candidates = sorted(
      {
          value.strip()
          for value in values if isinstance(value, str) and value.strip()
      },
      key=lambda value: (value.casefold(), value))
  selected_by_casefold = {}

  for value in candidates:
    selected_by_casefold.setdefault(value.casefold(), value)

  return sorted(selected_by_casefold.values(),
                key=lambda value: (value.casefold(), value))
