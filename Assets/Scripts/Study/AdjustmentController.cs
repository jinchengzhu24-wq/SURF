using System;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class AdjustmentController : MonoBehaviour
{
    [Header("Scene UI")]
    public InputField adjustmentInput;
    public Button submitButton;
    public Button detailButton;
    public Text statusText;

    [Header("Input")]
    public string adjustmentPlaceholder = "Describe how the level should be adjusted";
    [Min(1)]
    public int adjustmentCharacterLimit = 240;

    [Header("Flow")]
    public string customLevelSceneName = "Custom_Level";
    public string designInterpretationSceneName = "Design interpretation";
    public bool logAdjustmentEvents = true;

    private void Start()
    {
        ResolveSceneReferences();
        ConfigureInput();
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
            submitButton.interactable = !string.IsNullOrEmpty(AdjustmentText);
        }
    }

    private void Submit()
    {
        string adjustmentText = AdjustmentText;

        if (string.IsNullOrEmpty(adjustmentText))
        {
            return;
        }

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
