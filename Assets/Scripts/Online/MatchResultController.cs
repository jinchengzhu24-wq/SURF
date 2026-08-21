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

    [Header("Your Challenge / Opponent Run")]
    [SerializeField] private Text ownChallengeAssistantText;
    [SerializeField] private Text ownFeedbackText;
    [SerializeField] private Text opponentRunTimeText;
    [SerializeField] private Text opponentRunMovesText;

    [Header("Opponent Challenge / Your Run")]
    [SerializeField] private Text opponentChallengeAssistantText;
    [SerializeField] private Text opponentFeedbackText;
    [SerializeField] private Text ownRunTimeText;
    [SerializeField] private Text ownRunMovesText;

    [Header("Dynamic Feedback Text")]
    [SerializeField] private string ownFeedbackLabel = "YOUR MESSAGE";
    [SerializeField] private string opponentFeedbackLabel = "OPPONENT'S MESSAGE";
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
        ownFeedbackText = ResolveText(ownFeedbackText, "OwnFeedbackText");
        SetText(
            ownFeedbackText,
            FormatFeedbackText(ownFeedbackLabel, ownFeedbackDynamicText)
        );
    }

    public void SetOpponentFeedbackText(string value)
    {
        opponentFeedbackDynamicText = (value ?? "").Trim();
        opponentFeedbackText = ResolveText(
            opponentFeedbackText,
            "OpponentFeedbackText"
        );
        SetText(
            opponentFeedbackText,
            FormatFeedbackText(
                opponentFeedbackLabel,
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
            FormatFeedbackText(ownFeedbackLabel, ownFeedbackDynamicText)
        );
        SetText(
            opponentFeedbackText,
            FormatFeedbackText(
                opponentFeedbackLabel,
                opponentFeedbackDynamicText
            )
        );
    }

    private void Start()
    {
        OnlineSceneUi.EnsureEventSystem();
        ResolveUi();
        ConfigureResultTextLayout();
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

        RenderMetadata(
            state.ownChallengeMetadata,
            ownChallengeAssistantText
        );
        SetOwnFeedbackText(
            GetOpponentExperienceGoal(state.ownChallengeMetadata)
        );
        RenderMetadata(
            state.opponentChallengeMetadata,
            opponentChallengeAssistantText
        );
        SetOpponentFeedbackText(
            GetOpponentExperienceGoal(state.opponentChallengeMetadata)
        );
        RenderResult(
            state.ownResult,
            ownRunTimeText,
            ownRunMovesText
        );
        RenderResult(
            opponentSubmittedResult ? state.opponentResult : null,
            opponentRunTimeText,
            opponentRunMovesText
        );

        if (opponentSubmittedResult)
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

    private static void RenderMetadata(
        OnlineChallengeMetadata metadata,
        Text assistantText)
    {
        if (metadata == null)
        {
            SetText(assistantText, "AI MODE  --");
            return;
        }

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

        if (result.outcome == "timed_out")
        {
            SetText(timeText, "TIMEOUT  " + FormatDuration(result.durationSeconds));
            SetText(
                movesText,
                "MOVES  " + result.moveCount + " / MIN " + result.minimumMoves
            );
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

    private static string GetOpponentExperienceGoal(
        OnlineChallengeMetadata metadata)
    {
        return metadata != null
            ? (metadata.opponentExperienceGoal ?? "").Trim()
            : "";
    }

    private static string FormatFeedbackText(string label, string value)
    {
        string normalizedLabel = string.IsNullOrWhiteSpace(label)
            ? "MESSAGE"
            : label.Trim();
        return string.IsNullOrWhiteSpace(value)
            ? normalizedLabel + ":  --"
            : normalizedLabel + ":\n" + value.Trim();
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

    private void ResolveUi()
    {
        roomCodeText = ResolveText(roomCodeText, "RoomCodeText");
        statusText = ResolveText(statusText, "MatchResultStatusText");
        ownChallengeAssistantText = ResolveText(
            ownChallengeAssistantText,
            "OwnChallengeAssistantText"
        );
        ownFeedbackText = ResolveText(ownFeedbackText, "OwnFeedbackText");
        opponentRunTimeText = ResolveText(
            opponentRunTimeText,
            "OpponentRunTimeText"
        );
        opponentRunMovesText = ResolveText(
            opponentRunMovesText,
            "OpponentRunMovesText"
        );
        opponentChallengeAssistantText = ResolveText(
            opponentChallengeAssistantText,
            "OpponentChallengeAssistantText"
        );
        opponentFeedbackText = ResolveText(
            opponentFeedbackText,
            "OpponentFeedbackText"
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
            backToLobbyButton.onClick.RemoveListener(ContinueToQuestionnaire);
            backToLobbyButton.onClick.AddListener(ContinueToQuestionnaire);
        }
    }

    private void ConfigureResultTextLayout()
    {
        ConfigureResultCardTextLayout(
            opponentRunTimeText,
            opponentRunMovesText,
            ownFeedbackText
        );
        ConfigureResultCardTextLayout(
            ownRunTimeText,
            ownRunMovesText,
            opponentFeedbackText
        );
    }

    private static void ConfigureResultCardTextLayout(
        Text timeText,
        Text movesText,
        Text feedbackText)
    {
        ConfigureTextRect(
            timeText,
            new Vector2(-190f, -4f),
            new Vector2(310f, 55f)
        );
        ConfigureTextRect(
            movesText,
            new Vector2(155f, -4f),
            new Vector2(380f, 55f)
        );

        if (feedbackText == null)
        {
            return;
        }

        ConfigureTextRect(
            feedbackText,
            new Vector2(0f, -113f),
            new Vector2(690f, 145f)
        );
        feedbackText.alignment = TextAnchor.UpperLeft;
        feedbackText.horizontalOverflow = HorizontalWrapMode.Wrap;
        feedbackText.verticalOverflow = VerticalWrapMode.Truncate;
        feedbackText.resizeTextForBestFit = true;
        feedbackText.resizeTextMinSize = 18;
        feedbackText.resizeTextMaxSize = Mathf.Max(
            feedbackText.resizeTextMinSize,
            feedbackText.fontSize
        );
    }

    private static void ConfigureTextRect(
        Text text,
        Vector2 anchoredPosition,
        Vector2 sizeDelta)
    {
        if (text == null)
        {
            return;
        }

        text.rectTransform.anchoredPosition = anchoredPosition;
        text.rectTransform.sizeDelta = sizeDelta;
    }

    private void RenderUnavailable()
    {
        RenderMetadata(
            null,
            ownChallengeAssistantText
        );
        SetOwnFeedbackText("");
        RenderMetadata(
            null,
            opponentChallengeAssistantText
        );
        SetOpponentFeedbackText("");
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
