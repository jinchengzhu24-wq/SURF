using System;
using System.Collections;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class AdjustmentController : MonoBehaviour
{
    [Header("Scene UI")]
    public InputField adjustmentInput;
    public Button submitButton;
    public Button detailButton;
    public Text statusText;
    public Text clarificationGuidanceText;
    public Text clarificationScoreText;
    public Text clarificationConditionText;

    [Header("Human Clarification Text")]
    [TextArea(1, 3)]
    public string clarificationHeading = "Your revision needs clarification.";
    [TextArea(2, 5)]
    [Tooltip("Available tokens: {totalScore}, {problemScore}, {targetScore}, {directionScore}, {detailScore}, {reason}")]
    public string clarityScoreTemplate = "Clarity score: {totalScore}/8 (problem {problemScore}/2, target {targetScore}/2, direction {directionScore}/2, detail {detailScore}/2).";
    [TextArea(1, 3)]
    public string clarityPassConditionText = "Pass condition: total at least 4/8, target at least 1/2, and direction at least 1/2.";

    [Header("Input")]
    public string adjustmentPlaceholder = "Describe how the level should be adjusted";
    [Min(1)]
    public int adjustmentCharacterLimit = 240;

    [Header("Flow")]
    public string customLevelSceneName = "Custom_Level";
    public string designInterpretationSceneName = "Design interpretation";
    public string revisionMode = "";
    public bool validateHumanClarity;
    public bool loadPendingHumanAdjustment;
    public string clarificationSceneName = "Clarification(Human)";
    public string humanClarityEndpoint = "http://111.231.136.4:8000/validate-human-adjustment";
    public int clarityRequestTimeoutSeconds = 30;
    public bool logAdjustmentEvents = true;

    private bool isSubmitting;
    private UnityWebRequest activeClarityRequest;

    private void Start()
    {
        ResolveSceneReferences();
        ConfigureInput();
        ConfigureRevisionMode();
        RestorePendingHumanAdjustment();
        WireButtons();
        SetStatus("");
        UpdateSubmitState();
    }

    private void OnDestroy()
    {
        if (adjustmentInput != null)
        {
            adjustmentInput.onValueChanged.RemoveListener(OnAdjustmentChanged);
        }

        if (activeClarityRequest != null)
        {
            activeClarityRequest.Abort();
            activeClarityRequest.Dispose();
            activeClarityRequest = null;
        }
    }

    private void ResolveSceneReferences()
    {
        if (adjustmentInput == null)
        {
            GameObject inputObject = GameObject.Find("InputField");

            if (inputObject != null)
            {
                adjustmentInput = inputObject.GetComponent<InputField>();
            }
        }

        if (adjustmentInput == null)
        {
            adjustmentInput = FindObjectOfType<InputField>();
        }

        if (submitButton == null)
        {
            submitButton = FindButton("SubmitButton", "Submit");
        }

        if (detailButton == null)
        {
            detailButton = FindButton("DetailButton", "Detail");
        }

        if (statusText == null)
        {
            GameObject statusObject = GameObject.Find("StatusText");

            if (statusObject != null)
            {
                statusText = statusObject.GetComponent<Text>();
            }
        }
    }

    private void ConfigureInput()
    {
        if (adjustmentInput == null)
        {
            return;
        }

        adjustmentInput.contentType = InputField.ContentType.Standard;
        adjustmentInput.lineType = InputField.LineType.SingleLine;
        adjustmentInput.characterLimit = Mathf.Max(1, adjustmentCharacterLimit);

        if (adjustmentInput.textComponent != null)
        {
            adjustmentInput.textComponent.color = Color.black;
        }

        Text placeholderText = adjustmentInput.placeholder as Text;

        if (placeholderText != null && !string.IsNullOrEmpty(adjustmentPlaceholder))
        {
            placeholderText.text = adjustmentPlaceholder;
            placeholderText.color = new Color(0f, 0f, 0f, 0.45f);
        }
    }

    private void ConfigureRevisionMode()
    {
        if (string.IsNullOrWhiteSpace(revisionMode))
        {
            string sceneName = SceneManager.GetActiveScene().name.ToLowerInvariant();
            revisionMode = sceneName.Contains("(human)")
                ? "human"
                : sceneName.Contains("(ai)") ? "ai"
                : sceneName.Contains("(ha)") ? "ha" : "";
        }

        CreativeWorkshopContext.SetRevisionMode(revisionMode);
    }

    private void RestorePendingHumanAdjustment()
    {
        if (!loadPendingHumanAdjustment)
        {
            return;
        }

        string pendingText = PlayerPrefs.GetString(
            CreativeWorkshopContext.PendingHumanAdjustmentPrefsKey,
            ""
        );

        if (adjustmentInput != null && !string.IsNullOrWhiteSpace(pendingText))
        {
            adjustmentInput.text = pendingText;
        }

        string reason = PlayerPrefs.GetString(
            CreativeWorkshopContext.HumanClarityReasonPrefsKey,
            ""
        );

        if (!string.IsNullOrWhiteSpace(reason))
        {
            if (clarificationGuidanceText != null)
            {
                clarificationGuidanceText.text = clarificationHeading;
            }

            if (clarificationScoreText != null)
            {
                clarificationScoreText.text = reason.Trim();
            }

            if (clarificationConditionText != null)
            {
                clarificationConditionText.text = clarityPassConditionText;
            }
        }
    }

    private void WireButtons()
    {
        if (submitButton != null)
        {
            submitButton.onClick.RemoveAllListeners();
            submitButton.onClick.AddListener(Submit);
        }

        if (detailButton != null)
        {
            detailButton.onClick.RemoveAllListeners();
            detailButton.onClick.AddListener(LoadDesignInterpretation);
            detailButton.interactable = true;
        }

        if (adjustmentInput != null)
        {
            adjustmentInput.onValueChanged.RemoveListener(OnAdjustmentChanged);
            adjustmentInput.onValueChanged.AddListener(OnAdjustmentChanged);
        }
    }

    private void OnAdjustmentChanged(string value)
    {
        SetStatus("");
        UpdateSubmitState();
    }

    private void UpdateSubmitState()
    {
        if (submitButton != null)
        {
            submitButton.interactable = !isSubmitting && !string.IsNullOrEmpty(AdjustmentText);
        }
    }

    private void Submit()
    {
        string adjustmentText = AdjustmentText;

        if (string.IsNullOrEmpty(adjustmentText))
        {
            return;
        }

        if (validateHumanClarity)
        {
            StartCoroutine(ValidateHumanAdjustmentRoutine(adjustmentText));
            return;
        }

        AcceptAdjustmentAndContinue(adjustmentText);
    }

    private IEnumerator ValidateHumanAdjustmentRoutine(string adjustmentText)
    {
        isSubmitting = true;
        UpdateSubmitState();
        SetStatus("Checking whether the revision instruction is specific enough...");

        string separator = humanClarityEndpoint.Contains("?") ? "&" : "?";
        string url = humanClarityEndpoint
            + separator
            + "adjustmentText="
            + Uri.EscapeDataString(adjustmentText);
        activeClarityRequest = UnityWebRequest.Get(url);
        activeClarityRequest.timeout = Mathf.Max(1, clarityRequestTimeoutSeconds);
        yield return activeClarityRequest.SendWebRequest();

        if (activeClarityRequest.result != UnityWebRequest.Result.Success)
        {
            Debug.LogWarning(
                "AdjustmentController: Human clarity validation failed: "
                + activeClarityRequest.error
                + ", responseCode="
                + activeClarityRequest.responseCode
            );
            activeClarityRequest.Dispose();
            activeClarityRequest = null;
            isSubmitting = false;
            SetStatus("Clarity check failed. Please try again.");
            UpdateSubmitState();
            yield break;
        }

        HumanAdjustmentClarityResult result = null;

        try
        {
            result = JsonUtility.FromJson<HumanAdjustmentClarityResult>(
                activeClarityRequest.downloadHandler.text
            );
        }
        catch (Exception exception)
        {
            Debug.LogWarning(
                "AdjustmentController: Could not parse Human clarity result: "
                + exception.Message
            );
        }

        activeClarityRequest.Dispose();
        activeClarityRequest = null;
        isSubmitting = false;

        if (result == null)
        {
            SetStatus("Clarity check returned an invalid response. Please try again.");
            UpdateSubmitState();
            yield break;
        }

        if (!result.isClear)
        {
            string clarificationFeedback = loadPendingHumanAdjustment
                ? BuildClarificationFeedback(result)
                : "";
            CreativeWorkshopContext.SetPendingHumanAdjustment(
                adjustmentText,
                clarificationFeedback
            );

            if (logAdjustmentEvents)
            {
                Debug.Log(
                    "Human adjustment needs clarification:"
                    + " totalScore=" + result.totalScore + "/8"
                    + ", targetScore=" + result.targetScore
                    + ", directionScore=" + result.directionScore
                );
            }

            LoadScene(clarificationSceneName);
            yield break;
        }

        AcceptAdjustmentAndContinue(adjustmentText);
    }

    private string BuildClarificationFeedback(HumanAdjustmentClarityResult result)
    {
        string scoreText = (clarityScoreTemplate ?? "")
            .Replace("{totalScore}", result.totalScore.ToString())
            .Replace("{problemScore}", result.problemScore.ToString())
            .Replace("{targetScore}", result.targetScore.ToString())
            .Replace("{directionScore}", result.directionScore.ToString())
            .Replace("{detailScore}", result.detailScore.ToString())
            .Replace("{reason}", CleanText(result.reason));

        return scoreText;
    }

    private void AcceptAdjustmentAndContinue(string adjustmentText)
    {
        CreativeWorkshopContext.SetRevisionMode(revisionMode);
        CreativeWorkshopContext.ClearSelectedHAPlan();
        CapturePreviousLevelPlan();
        CreativeWorkshopContext.ClearPendingHumanAdjustment();

        string ideaId = GetContextValue(
            CreativeWorkshopContext.IdeaId,
            CreativeWorkshopContext.IdeaIdPrefsKey
        );
        string sessionId = GetContextValue(
            CreativeWorkshopContext.SessionId,
            CreativeWorkshopContext.SessionIdPrefsKey
        );
        string currentIdeaText = GetContextValue(
            CreativeWorkshopContext.IdeaText,
            CreativeWorkshopContext.IdeaTextPrefsKey
        );
        string adjustedIdeaText = BuildAdjustedIdeaText(currentIdeaText, adjustmentText);

        CreativeWorkshopContext.AppendAdjustment(
            ideaId,
            sessionId,
            adjustmentText,
            adjustedIdeaText
        );
        LevelStudyRecorder.UpdateCustomRoundIdea(ideaId, adjustedIdeaText);
        LevelStudyRecorder.RecordJourneyStage(
            "adjustment",
            "submitted",
            adjustmentText,
            revisionMode
        );

        if (logAdjustmentEvents)
        {
            Debug.Log(
                "Adjustment submitted:"
                + " ideaId=" + ideaId
                + ", adjustmentLength=" + adjustmentText.Length
            );
        }

        LoadScene(customLevelSceneName);
    }

    private void CapturePreviousLevelPlan()
    {
        LevelDesignPlan previousPlan;

        if (LevelDesignPlanContext.TryGetPlan(out previousPlan) && previousPlan != null)
        {
            CreativeWorkshopContext.SetPreviousLevelPlan(JsonUtility.ToJson(previousPlan));
        }
        else
        {
            CreativeWorkshopContext.SetPreviousLevelPlan("");
        }
    }

    private string BuildAdjustedIdeaText(string currentIdeaText, string adjustmentText)
    {
        string cleanCurrentIdeaText = CleanText(currentIdeaText);
        string adjustmentFeedback = "User adjustment: " + CleanText(adjustmentText);

        return string.IsNullOrEmpty(cleanCurrentIdeaText)
            ? adjustmentFeedback
            : cleanCurrentIdeaText + " " + adjustmentFeedback;
    }

    private void LoadDesignInterpretation()
    {
        DesignInterpretationController.SetReturnScene(SceneManager.GetActiveScene().name);
        LoadScene(designInterpretationSceneName);
    }

    private void LoadScene(string sceneName)
    {
        if (string.IsNullOrEmpty(sceneName))
        {
            Debug.LogWarning("AdjustmentController: scene name is empty.");
            return;
        }

        SceneManager.LoadScene(sceneName);
    }

    private Button FindButton(string objectName, string label)
    {
        GameObject buttonObject = GameObject.Find(objectName);

        if (buttonObject != null)
        {
            Button namedButton = buttonObject.GetComponent<Button>();

            if (namedButton != null)
            {
                return namedButton;
            }
        }

        Button[] buttons = FindObjectsOfType<Button>();

        for (int i = 0; i < buttons.Length; i++)
        {
            Text buttonText = buttons[i].GetComponentInChildren<Text>();

            if (buttonText != null && CleanText(buttonText.text) == label)
            {
                return buttons[i];
            }
        }

        return null;
    }

    private string GetContextValue(string runtimeValue, string prefsKey)
    {
        return !string.IsNullOrEmpty(runtimeValue)
            ? runtimeValue
            : PlayerPrefs.GetString(prefsKey, "");
    }

    private void SetStatus(string message)
    {
        if (statusText != null)
        {
            statusText.text = message;
        }
    }

    private string CleanText(string value)
    {
        return string.IsNullOrEmpty(value)
            ? ""
            : string.Join(" ", value.Trim().Split((char[])null, StringSplitOptions.RemoveEmptyEntries));
    }

    private string AdjustmentText
    {
        get
        {
            return adjustmentInput != null && adjustmentInput.text != null
                ? adjustmentInput.text.Trim()
                : "";
        }
    }
}

[Serializable]
public class HumanAdjustmentClarityResult
{
    public int problemScore;
    public int targetScore;
    public int directionScore;
    public int detailScore;
    public int totalScore;
    public bool isClear;
    public string reason;
}
