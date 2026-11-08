using System.Collections;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class MatchResultController : MonoBehaviour
{
    private const string QuestionnaireSceneName = "Questionnaire(Online2)";

    [Header("Header")]
    [SerializeField] private Text roomCodeText;
    [SerializeField] private Text statusText;
    [SerializeField] private Text finishedCountText;
    [SerializeField] private Text footerText;

    [Header("Your Challenge / Opponent Run")]
    [SerializeField] private Text ownFeedbackText;
    [SerializeField] private Text opponentRunTimeText;
    [SerializeField] private Text opponentRunMovesText;
    [SerializeField] private Text opponentRunStatusText;

    [Header("Opponent Challenge / Your Run")]
    [SerializeField] private Text opponentFeedbackText;
    [SerializeField] private Text ownRunTimeText;
    [SerializeField] private Text ownRunMovesText;
    [SerializeField] private Text ownRunStatusText;

    [Header("Runtime Values (Play Mode)")]
    [SerializeField, TextArea(2, 6)]
    private string ownFeedbackDynamicText = "";
    [SerializeField, TextArea(2, 6)]
    private string opponentFeedbackDynamicText = "";

    [Header("Actions")]
    [SerializeField] private Button backToLobbyButton;

    private OnlineMatchClient client;
    private bool leaving;

    public string OwnFeedbackValue => ownFeedbackDynamicText;

    public string OpponentFeedbackValue => opponentFeedbackDynamicText;

    public void SetOwnFeedbackText(string value)
    {
        ownFeedbackDynamicText = (value ?? "").Trim();
        SetText(
            ownFeedbackText,
            FormatFeedbackText(ownFeedbackDynamicText)
        );
    }

    public void SetOpponentFeedbackText(string value)
    {
        opponentFeedbackDynamicText = (value ?? "").Trim();
        SetText(
            opponentFeedbackText,
            FormatFeedbackText(
                opponentFeedbackDynamicText
            )
        );
    }

    public void SetFeedbackTexts(string ownValue, string opponentValue)
    {
        SetOwnFeedbackText(ownValue);
        SetOpponentFeedbackText(opponentValue);
    }

    private void OnValidate()
    {
        SetText(
            ownFeedbackText,
            FormatFeedbackText(ownFeedbackDynamicText)
        );
        SetText(
            opponentFeedbackText,
            FormatFeedbackText(
                opponentFeedbackDynamicText
            )
        );
    }

    private void Start()
    {
        OnlineSceneUi.EnsureEventSystem();
        WireUi();

        SetText(roomCodeText, "ROOM " + (string.IsNullOrEmpty(OnlineMatchContext.RoomCode)
            ? "------" : OnlineMatchContext.RoomCode));

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

        if (!HasOpponentSubmittedResult(OnlineMatchContext.RoomState))
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
                    HasOpponentSubmittedResult(OnlineMatchContext.RoomState)
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

        bool opponentSubmittedResult = HasOpponentSubmittedResult(state);
        bool ownSubmittedResult = state.FindPlayer(state.playerNumber)
            ?.resultSubmitted == true;
        int finishedCount = (ownSubmittedResult ? 1 : 0)
            + (opponentSubmittedResult ? 1 : 0);
        SetText(finishedCountText, finishedCount + "/2 FINISHED");

        SetOwnFeedbackText(
            GetDesignerIntention(state.ownChallengeMetadata)
        );
        SetOpponentFeedbackText(
            GetDesignerIntention(state.opponentChallengeMetadata)
        );
        RenderResult(
            state.ownResult,
            ownRunTimeText,
            ownRunMovesText,
            ownRunStatusText,
            "YOU"
        );
        RenderResult(
            opponentSubmittedResult ? state.opponentResult : null,
            opponentRunTimeText,
            opponentRunMovesText,
            opponentRunStatusText,
            "PARTNER"
        );

        if (state.status == "cancelled")
        {
            SetText(statusText, "YOUR PARTNER LEFT THE MATCH.");
            if (!opponentSubmittedResult)
            {
                SetText(opponentRunStatusText, "PARTNER LEFT THE MATCH.");
            }
            SetText(footerText, "Continue to the post-match questionnaire.");
        }
        else if (finishedCount == 2)
        {
            SetText(statusText, "BOTH PLAYERS HAVE FINISHED.");
            SetText(footerText, "Continue to the post-match questionnaire.");
        }
        else
        {
            SetText(statusText, opponentSubmittedResult
                ? "YOUR PARTNER HAS FINISHED."
                : "YOUR PARTNER IS STILL PLAYING...");
            SetText(footerText, "More results appear when both players finish.");
        }
    }

    private static bool HasOpponentSubmittedResult(OnlineRoomState state)
    {
        int playerNumber = state != null ? state.playerNumber : 0;

        if (state == null || playerNumber < 1 || playerNumber > 2)
        {
            return false;
        }

        OnlinePlayerState opponent = state.FindPlayer(3 - playerNumber);
        return opponent != null && opponent.resultSubmitted;
    }

    private static void RenderResult(
        OnlineMatchResult result,
        Text timeText,
        Text movesText,
        Text runStatusText,
        string player)
    {
        if (result == null)
        {
            SetText(timeText, "--");
            SetText(movesText, "-- / --");
            SetText(runStatusText, player == "YOU"
                ? "WAITING FOR YOUR RESULT..."
                : "PARTNER IS PLAYING...");
            return;
        }

        if (result.outcome == "timed_out")
        {
            SetText(runStatusText, player == "YOU" ? "YOU TIMED OUT." : "PARTNER TIMED OUT.");
            SetText(timeText, FormatDuration(result.durationSeconds));
            SetText(
                movesText,
                result.moveCount + " / " + result.minimumMoves
            );
            return;
        }

        SetText(runStatusText, player == "YOU" ? "YOU FINISHED!" : "PARTNER FINISHED!");
        SetText(timeText, FormatDuration(result.durationSeconds));
        SetText(
            movesText,
            result.moveCount
            + " / "
            + result.minimumMoves
        );
    }

    private static string GetDesignerIntention(
        OnlineChallengeMetadata metadata)
    {
        return metadata != null
            ? (metadata.designerIntention ?? "").Trim()
            : "";
    }

    private static string FormatFeedbackText(string value)
    {
        return string.IsNullOrWhiteSpace(value)
            ? "No message yet."
            : value.Trim();
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

    private void ContinueToQuestionnaire()
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
            FinishQuestionnaireTransition();
            return;
        }

        StartCoroutine(
            client.LeaveRoom(
                state => FinishQuestionnaireTransition(),
                error => FinishQuestionnaireTransition()
            )
        );
    }

    private void FinishQuestionnaireTransition()
    {
        OnlineMatchContext.StagePostMatchSurvey();
        OnlineMatchContext.Clear();
        SceneManager.LoadScene(QuestionnaireSceneName);
    }

    private void WireUi()
    {
        if (backToLobbyButton != null)
        {
            backToLobbyButton.onClick.RemoveListener(ContinueToQuestionnaire);
            backToLobbyButton.onClick.AddListener(ContinueToQuestionnaire);
        }
    }

    private void RenderUnavailable()
    {
        SetOwnFeedbackText("");
        SetOpponentFeedbackText("");
        RenderResult(null, opponentRunTimeText, opponentRunMovesText, opponentRunStatusText, "PARTNER");
        RenderResult(null, ownRunTimeText, ownRunMovesText, ownRunStatusText, "YOU");
        SetText(opponentRunStatusText, "RESULT UNAVAILABLE");
        SetText(ownRunStatusText, "RESULT UNAVAILABLE");
        SetText(finishedCountText, "--/2 FINISHED");
        SetText(footerText, "Continue to the post-match questionnaire.");
    }

    private static void SetText(Text text, string value)
    {
        if (text != null)
        {
            text.text = value;
        }
    }
}
