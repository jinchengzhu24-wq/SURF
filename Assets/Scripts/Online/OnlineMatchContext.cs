using System;

public static class OnlineMatchContext
{
    public static string MatchId { get; private set; } = "";
    public static string RoomCode { get; private set; } = "";
    public static string PlayerToken { get; private set; } = "";
    public static int PlayerNumber { get; private set; }
    public static OnlineRoomState RoomState { get; private set; }

    public static bool HasMatch =>
        !string.IsNullOrWhiteSpace(MatchId)
        && !string.IsNullOrWhiteSpace(PlayerToken)
        && (PlayerNumber == 1 || PlayerNumber == 2);

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
        RoomState = state;
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
    }

    public static void Clear()
    {
        MatchId = "";
        RoomCode = "";
        PlayerToken = "";
        PlayerNumber = 0;
        RoomState = null;
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
}
