using System;

public static class OnlineMatchContext
{
    public static string MatchId { get; private set; } = "";
    public static string RoomCode { get; private set; } = "";
    public static string PlayerToken { get; private set; } = "";
    public static int PlayerNumber { get; private set; }
    public static OnlineRoomState RoomState { get; private set; }
    public static string[] PendingChallengeRows { get; private set; }
    public static string PendingAiAssistantMode { get; private set; } = "";
    public static string[] OpponentChallengeRows { get; private set; }
    public static string PendingSurveyMatchId { get; private set; } = "";
    public static string PendingSurveyRoomCode { get; private set; } = "";
    public static int PendingSurveyPlayerNumber { get; private set; }

    public static bool HasMatch =>
        !string.IsNullOrWhiteSpace(MatchId)
        && !string.IsNullOrWhiteSpace(PlayerToken)
        && (PlayerNumber == 1 || PlayerNumber == 2);

    public static bool HasPendingChallenge =>
        PendingChallengeRows != null && PendingChallengeRows.Length > 0;

    public static bool HasOpponentChallenge =>
        OpponentChallengeRows != null && OpponentChallengeRows.Length > 0;

    public static bool HasPendingPostMatchSurvey =>
        !string.IsNullOrWhiteSpace(PendingSurveyMatchId)
        && (PendingSurveyPlayerNumber == 1 || PendingSurveyPlayerNumber == 2);

    public static void Initialize(OnlineRoomState state)
    {
        if (state == null
            || string.IsNullOrWhiteSpace(state.matchId)
            || string.IsNullOrWhiteSpace(state.playerToken)
            || (state.playerNumber != 1 && state.playerNumber != 2))
        {
            throw new ArgumentException("Online room response is missing player identity.");
        }

        MatchId = state.matchId;
        RoomCode = state.roomCode ?? "";
        PlayerToken = state.playerToken;
        PlayerNumber = state.playerNumber;
        ClearPendingPostMatchSurvey();
        RoomState = state;
        PendingChallengeRows = null;
        PendingAiAssistantMode = "";
        OpponentChallengeRows = CloneRows(state.opponentChallengeRows);
    }

    public static void ApplyState(OnlineRoomState state)
    {
        if (state == null || !HasMatch || state.matchId != MatchId)
        {
            return;
        }

        state.playerToken = PlayerToken;
        state.playerNumber = PlayerNumber;
        RoomCode = state.roomCode ?? RoomCode;
        RoomState = state;

        if (state.opponentChallengeRows != null)
        {
            OpponentChallengeRows = CloneRows(state.opponentChallengeRows);
        }
    }

    public static void StageChallenge(
        string[] rows,
        string aiAssistantMode)
    {
        if (!HasMatch || rows == null || rows.Length == 0)
        {
            throw new ArgumentException(
                "An active online room and challenge rows are required."
            );
        }

        PendingChallengeRows = CloneRows(rows);
        PendingAiAssistantMode = aiAssistantMode ?? "";
    }

    public static void ClearPendingChallenge()
    {
        PendingChallengeRows = null;
        PendingAiAssistantMode = "";
    }

    public static void StagePostMatchSurvey()
    {
        if (!HasMatch)
        {
            ClearPendingPostMatchSurvey();
            return;
        }

        PendingSurveyMatchId = MatchId;
        PendingSurveyRoomCode = RoomCode;
        PendingSurveyPlayerNumber = PlayerNumber;
    }

    public static void ClearPendingPostMatchSurvey()
    {
        PendingSurveyMatchId = "";
        PendingSurveyRoomCode = "";
        PendingSurveyPlayerNumber = 0;
    }

    public static void Clear()
    {
        MatchId = "";
        RoomCode = "";
        PlayerToken = "";
        PlayerNumber = 0;
        RoomState = null;
        PendingChallengeRows = null;
        PendingAiAssistantMode = "";
        OpponentChallengeRows = null;
    }

    private static string[] CloneRows(string[] rows)
    {
        if (rows == null)
        {
            return null;
        }

        string[] clone = new string[rows.Length];

        for (int row = 0; row < rows.Length; row++)
        {
            clone[row] = rows[row] ?? "";
        }

        return clone;
    }
}

[Serializable]
public class OnlineRoomState
{
    public string matchId;
    public string roomCode;
    public string playerToken;
    public int playerNumber;
    public string status;
    public OnlinePlayerState[] players;
    public string[] opponentChallengeRows;
    public OnlineChallengeMetadata ownChallengeMetadata;
    public OnlineChallengeMetadata opponentChallengeMetadata;
    public OnlineMatchResult ownResult;
    public OnlineMatchResult opponentResult;

    public OnlinePlayerState FindPlayer(int number)
    {
        if (players == null)
        {
            return null;
        }

        for (int i = 0; i < players.Length; i++)
        {
            if (players[i] != null && players[i].playerNumber == number)
            {
                return players[i];
            }
        }

        return null;
    }
}

[Serializable]
public class OnlinePlayerState
{
    public int playerNumber;
    public bool ready;
    public bool challengeSubmitted;
    public bool resultSubmitted;
}

[Serializable]
public class OnlineChallengeMetadata
{
    public string aiAssistantMode;
    public string designerIntention;
}

[Serializable]
public class OnlineMatchResult
{
    public float durationSeconds;
    public int moveCount;
    public int minimumMoves;
    public string outcome;
}
