using System.Collections;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class MatchResultController : MonoBehaviour
{
    private const string LobbySceneName = "Online_Lobby";

    [Header("Header")]
    [SerializeField] private Text roomCodeText;
    [SerializeField] private Text statusText;

    [Header("Your Challenge / Opponent Run")]
    [SerializeField] private Text ownChallengeCompetitionText;
    [SerializeField] private Text ownChallengeAssistantText;
    [SerializeField] private Text opponentRunTimeText;
    [SerializeField] private Text opponentRunMovesText;

    [Header("Opponent Challenge / Your Run")]
    [SerializeField] private Text opponentChallengeCompetitionText;
    [SerializeField] private Text opponentChallengeAssistantText;
    [SerializeField] private Text ownRunTimeText;
    [SerializeField] private Text ownRunMovesText;

    [Header("Actions")]
    [SerializeField] private Button backToLobbyButton;

    private OnlineMatchClient client;
    private bool leaving;

    private void Start()
    {
        OnlineSceneUi.EnsureEventSystem();
        ResolveUi();
        WireUi();
        OnlineSceneUi.ConfigureRaycastTargets();

        SetText(roomCodeText, "ROOM " + OnlineMatchContext.RoomCode);

        if (!OnlineMatchContext.HasMatch
            || OnlineMatchContext.RoomState == null)
        {
            SetText(statusText, "NO ACTIVE MATCH RESULT WAS FOUND.");
            RenderUnavailable();
            return;
        }

        client = GetComponent<OnlineMatchClient>();

        if (client == null)
        {
            client = gameObject.AddComponent<OnlineMatchClient>();
        }

        RenderState(OnlineMatchContext.RoomState);

        if (OnlineMatchContext.RoomState.opponentResult == null)
        {
            StartCoroutine(PollUntilOpponentFinishes());
        }
    }

    private IEnumerator PollUntilOpponentFinishes()
    {
        while (!leaving && OnlineMatchContext.HasMatch)
        {
            bool receivedState = false;

            yield return client.GetRoom(
                state =>
                {
                    receivedState = true;
                    OnlineMatchContext.ApplyState(state);
                    RenderState(state);
                },
                error => SetText(
                    statusText,
                    "CONNECTION LOST. RETRYING...\n"
                    + error.ToUpperInvariant()
                )
            );

            if (receivedState
                && OnlineMatchContext.RoomState != null
                && (
                    OnlineMatchContext.RoomState.opponentResult != null
                    || OnlineMatchContext.RoomState.status == "cancelled"
                ))
            {
                yield break;
            }

            yield return new WaitForSecondsRealtime(1f);
        }
    }

    private void RenderState(OnlineRoomState state)
    {
        if (state == null)
        {
            RenderUnavailable();
            return;
        }

        RenderMetadata(
            state.ownChallengeMetadata,
            ownChallengeCompetitionText,
            ownChallengeAssistantText
        );
        RenderMetadata(
            state.opponentChallengeMetadata,
            opponentChallengeCompetitionText,
            opponentChallengeAssistantText
        );
        RenderResult(
            state.ownResult,
            ownRunTimeText,
            ownRunMovesText
        );
        RenderResult(
            state.opponentResult,
            opponentRunTimeText,
            opponentRunMovesText
        );

        if (state.opponentResult != null)
        {
            SetText(statusText, "BOTH PLAYERS HAVE FINISHED.");
        }
        else if (state.status == "cancelled")
        {
            SetText(statusText, "YOUR OPPONENT LEFT THE MATCH.");
        }
        else
        {
            SetText(statusText, "WAITING FOR OPPONENT TO FINISH...");
        }
    }

    private static void RenderMetadata(
        OnlineChallengeMetadata metadata,
        Text competitionText,
        Text assistantText)
    {
        if (metadata == null)
        {
            SetText(competitionText, "BUILD MODE  --");
            SetText(assistantText, "AI MODE  --");
            return;
        }

        SetText(
            competitionText,
            "BUILD MODE  " + FormatCompetitionMode(metadata.competitionMode)
        );
        SetText(
            assistantText,
            "AI MODE  " + FormatAssistantMode(metadata.aiAssistantMode)
        );
    }

    private static void RenderResult(
        OnlineMatchResult result,
        Text timeText,
        Text movesText)
    {
        if (result == null)
        {
            SetText(timeText, "TIME  --");
            SetText(movesText, "MOVES  -- / MIN --");
            return;
        }

        SetText(timeText, "TIME  " + FormatDuration(result.durationSeconds));
        SetText(
            movesText,
            "MOVES  "
            + result.moveCount
            + " / MIN "
            + result.minimumMoves
        );
    }

    private static string FormatCompetitionMode(string mode)
    {
        return mode == CompetitionModeController.SupportiveModeId
            ? "SUPPORTIVE"
            : "COMPETITIVE";
    }

    private static string FormatAssistantMode(string mode)
    {
        return mode == AIAssistantModeController.PartialCompletionApiMode
            ? "PARTIAL-LEVEL COMPLETION"
            : "DESCRIPTION-TO-LEVEL";
    }

    private static string FormatDuration(float seconds)
    {
        int totalHundredths = Mathf.Max(
            0,
            Mathf.RoundToInt(seconds * 100f)
        );
        int minutes = totalHundredths / 6000;
        int remainingSeconds = (totalHundredths / 100) % 60;
        int hundredths = totalHundredths % 100;
        return minutes.ToString("00")
            + ":"
            + remainingSeconds.ToString("00")
            + "."
            + hundredths.ToString("00");
    }

    private void BackToLobby()
    {
        if (leaving)
        {
            return;
        }

        leaving = true;

        if (backToLobbyButton != null)
        {
            backToLobbyButton.interactable = false;
        }

        if (client == null || !OnlineMatchContext.HasMatch)
        {
            FinishLeaving();
            return;
        }

        StartCoroutine(
            client.LeaveRoom(
                state => FinishLeaving(),
                error => FinishLeaving()
            )
        );
    }

    private void FinishLeaving()
    {
        OnlineMatchContext.Clear();
        SceneManager.LoadScene(LobbySceneName);
    }

    private void ResolveUi()
    {
        roomCodeText = ResolveText(roomCodeText, "RoomCodeText");
        statusText = ResolveText(statusText, "MatchResultStatusText");
        ownChallengeCompetitionText = ResolveText(
            ownChallengeCompetitionText,
            "OwnChallengeCompetitionText"
        );
        ownChallengeAssistantText = ResolveText(
            ownChallengeAssistantText,
            "OwnChallengeAssistantText"
        );
        opponentRunTimeText = ResolveText(
            opponentRunTimeText,
            "OpponentRunTimeText"
        );
        opponentRunMovesText = ResolveText(
            opponentRunMovesText,
            "OpponentRunMovesText"
        );
        opponentChallengeCompetitionText = ResolveText(
            opponentChallengeCompetitionText,
            "OpponentChallengeCompetitionText"
        );
        opponentChallengeAssistantText = ResolveText(
            opponentChallengeAssistantText,
            "OpponentChallengeAssistantText"
        );
        ownRunTimeText = ResolveText(ownRunTimeText, "OwnRunTimeText");
        ownRunMovesText = ResolveText(ownRunMovesText, "OwnRunMovesText");
        backToLobbyButton = backToLobbyButton != null
            ? backToLobbyButton
            : OnlineSceneUi.EnsureButton("BackToLobbyButton");
    }

    private void WireUi()
    {
        if (backToLobbyButton != null)
        {
            backToLobbyButton.onClick.RemoveListener(BackToLobby);
            backToLobbyButton.onClick.AddListener(BackToLobby);
        }
    }

    private void RenderUnavailable()
    {
        RenderMetadata(
            null,
            ownChallengeCompetitionText,
            ownChallengeAssistantText
        );
        RenderMetadata(
            null,
            opponentChallengeCompetitionText,
            opponentChallengeAssistantText
        );
        RenderResult(null, opponentRunTimeText, opponentRunMovesText);
        RenderResult(null, ownRunTimeText, ownRunMovesText);
    }

    private static Text ResolveText(Text current, string objectName)
    {
        return current != null
            ? current
            : OnlineSceneUi.FindText(objectName);
    }

    private static void SetText(Text text, string value)
    {
        if (text != null)
        {
            text.text = value;
        }
    }
}
