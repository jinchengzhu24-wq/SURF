using System;
using System.Collections;
using System.Text;
using TMPro;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class HAPlanController : MonoBehaviour
{
    private const string DefaultBackendBaseUrl = "http://111.231.136.4:8000";
    private const string GeneratePath = "/generate-ha-revision-plans";
    private const string ChoicePath = "/record-ha-plan-choice";

    [Header("Scene UI")]
    public Button submitButton;
    public Button regenerateButton;
    public Text statusText;
    public Text guidanceText;
    public Text originalIdeaText;
    public Text runtimeStatusText;
    public Transform optionsParent;
    public Button[] optionButtons = new Button[3];
    public Image[] optionCardImages = new Image[3];
    public Text[] optionTitleTexts = new Text[3];
    public Text[] optionDescriptionTexts = new Text[3];

    [Header("Flow")]
    public string nextSceneName = "Custom_Level";

    [Header("Backend")]
    public string backendBaseUrl = DefaultBackendBaseUrl;
    public int requestTimeoutSeconds = 30;
    public bool logPlanEvents = true;

    private readonly Color normalCardColor = new Color(0.95f, 0.97f, 1f, 1f);
    private readonly Color selectedCardColor = new Color(0.78f, 0.88f, 1f, 1f);
    private readonly Color disabledCardColor = new Color(0.86f, 0.86f, 0.86f, 1f);
    private readonly ScrollRect[] optionDescriptionScrollRects = new ScrollRect[3];

    private HARevisionPlanOption[] options = new HARevisionPlanOption[0];
    private HARevisionPlanOption[] lastSuccessfulOptions = new HARevisionPlanOption[0];
    private string adjustmentText = "";
    private LevelDesignPlan previousLevelPlan;
    private CorridorValidationResult corridorValidation;
    private bool isRequesting;
    private bool isContinuing;
    private bool requestFailed;
    private int selectedOptionIndex = -1;
    private int regenerationAttempt;

    private void Start()
    {
        EnsureUiArrays();
        ResolveSceneReferences();
        ResolveOptionDescriptionScrollRects();
        WireButtons();
        LoadContext();
        RefreshStaticText();
        UpdateButtonState();

        if (HasRequiredContext())
        {
            StartCoroutine(RequestPlansRoutine());
        }
        else
        {
            requestFailed = true;
            SetStatus("The previous level plan or HA adjustment is missing. Retry after restoring the context.");
            UpdateButtonState();
        }
    }

    private void OnDestroy()
    {
        if (submitButton != null)
        {
            submitButton.onClick.RemoveListener(ConfirmSelection);
        }

        if (regenerateButton != null)
        {
            regenerateButton.onClick.RemoveListener(Regenerate);
        }

        if (optionButtons == null)
        {
            return;
        }

        for (int i = 0; i < optionButtons.Length; i++)
        {
            if (optionButtons[i] != null)
            {
                optionButtons[i].onClick.RemoveAllListeners();
            }
        }
    }

    private void EnsureUiArrays()
    {
        if (optionButtons == null || optionButtons.Length < 3)
        {
            optionButtons = new Button[3];
        }

        if (optionCardImages == null || optionCardImages.Length < 3)
        {
            optionCardImages = new Image[3];
        }

        if (optionTitleTexts == null || optionTitleTexts.Length < 3)
        {
            optionTitleTexts = new Text[3];
        }

        if (optionDescriptionTexts == null || optionDescriptionTexts.Length < 3)
        {
            optionDescriptionTexts = new Text[3];
        }
    }

    private void ResolveSceneReferences()
    {
        if (submitButton == null)
        {
            submitButton = FindButton("SubmitButton", "Submit");
        }

        if (regenerateButton == null)
        {
            regenerateButton = FindButton("Regenerate", "Regenerate");
        }

        if (guidanceText == null)
        {
            guidanceText = FindText("Guidance");
        }

        if (statusText == null)
        {
            statusText = FindText("StatusText");
        }

        if (originalIdeaText == null)
        {
            originalIdeaText = FindText("OriginalIdeaText");
        }

        if (runtimeStatusText == null)
        {
            runtimeStatusText = FindText("ExpansionStatusText");
        }

        if (optionsParent == null)
        {
            GameObject optionsObject = GameObject.Find("ExpansionOptionsPanel");
            optionsParent = optionsObject != null ? optionsObject.transform : null;
        }

        ResolveOptionReferences();
    }

    private void ResolveOptionReferences()
    {
        for (int i = 0; i < 3; i++)
        {
            GameObject optionObject = GameObject.Find("ExpansionOption" + (i + 1));

            if (optionObject == null)
            {
                continue;
            }

            if (optionButtons[i] == null)
            {
                optionButtons[i] = optionObject.GetComponent<Button>();
            }

            if (optionCardImages[i] == null)
            {
                optionCardImages[i] = optionObject.GetComponent<Image>();
            }

            if (optionTitleTexts[i] == null)
            {
                Transform title = optionObject.transform.Find("TitleText");
                optionTitleTexts[i] = title != null ? title.GetComponent<Text>() : null;
            }

            if (optionDescriptionTexts[i] == null)
            {
                Transform description = optionObject.transform.Find(
                    "DescriptionScrollView/Viewport/DescriptionText"
                );
                optionDescriptionTexts[i] = description != null
                    ? description.GetComponent<Text>()
                    : null;
            }
        }
    }

    private void ResolveOptionDescriptionScrollRects()
    {
        for (int i = 0; i < optionDescriptionTexts.Length; i++)
        {
            optionDescriptionScrollRects[i] = optionDescriptionTexts[i] != null
                ? optionDescriptionTexts[i].GetComponentInParent<ScrollRect>()
                : null;
        }
    }

    private void WireButtons()
    {
        if (submitButton != null)
        {
            submitButton.onClick.RemoveAllListeners();
            submitButton.onClick.AddListener(ConfirmSelection);
        }

        if (regenerateButton != null)
        {
            regenerateButton.onClick.RemoveAllListeners();
            regenerateButton.onClick.AddListener(Regenerate);
        }

        for (int i = 0; i < optionButtons.Length; i++)
        {
            int optionIndex = i;

            if (optionButtons[i] != null)
            {
                optionButtons[i].onClick.RemoveAllListeners();
                optionButtons[i].onClick.AddListener(() => SelectOption(optionIndex));
            }
        }
    }

    private void LoadContext()
    {
        adjustmentText = PlayerPrefs.GetString(
            CreativeWorkshopContext.LatestAdjustmentTextPrefsKey,
            ""
        ).Trim();
        CreativeWorkshopContext.SetRevisionMode("ha");

        LevelDesignPlan storedPlan;

        if (LevelDesignPlanContext.TryGetPlan(out storedPlan) && storedPlan != null)
        {
            previousLevelPlan = storedPlan;
            corridorValidation = LevelDesignPlanContext.CorridorValidation;
            return;
        }

        string previousPlanJson = PlayerPrefs.GetString(
            CreativeWorkshopContext.PreviousLevelPlanPrefsKey,
            ""
        );

        if (!string.IsNullOrWhiteSpace(previousPlanJson))
        {
            try
            {
                previousLevelPlan = JsonUtility.FromJson<LevelDesignPlan>(previousPlanJson);
            }
            catch (Exception exception)
            {
                Debug.LogWarning("HAPlanController could not parse previous plan: " + exception.Message);
            }
        }
    }

    private void RefreshStaticText()
    {
        if (originalIdeaText != null)
        {
            originalIdeaText.text = string.IsNullOrEmpty(adjustmentText)
                ? "Your adjustment: missing"
                : "Your adjustment: " + adjustmentText;
        }

        SetOptionTexts(null);
        SetStatus("");
    }

    private bool HasRequiredContext()
    {
        return !string.IsNullOrWhiteSpace(adjustmentText) && previousLevelPlan != null;
    }

    private IEnumerator RequestPlansRoutine(
        bool isRegeneration = false,
        HARevisionPlanOption[] previousOptions = null)
    {
        if (isRequesting || isContinuing)
        {
            yield break;
        }

        LoadContext();

        if (!HasRequiredContext())
        {
            requestFailed = true;
            options = new HARevisionPlanOption[0];
            selectedOptionIndex = -1;
            SetOptionTexts(null);
            SetStatus("The previous level plan or HA adjustment is missing. Retry after restoring the context.");
            UpdateButtonState();
            yield break;
        }

        isRequesting = true;
        requestFailed = false;
        selectedOptionIndex = -1;
        options = new HARevisionPlanOption[0];
        SetOptionTexts(null);
        SetStatus(isRegeneration ? "Regenerating revision plans..." : "Generating revision plans...");
        UpdateButtonState();

        HARevisionPlanRequest requestBody = new HARevisionPlanRequest
        {
            ideaId = GetContextValue(
                CreativeWorkshopContext.IdeaId,
                CreativeWorkshopContext.IdeaIdPrefsKey
            ),
            sessionId = GetContextValue(
                CreativeWorkshopContext.SessionId,
                CreativeWorkshopContext.SessionIdPrefsKey
            ),
            adjustmentText = adjustmentText,
            sceneName = SceneManager.GetActiveScene().name,
            previousLevelPlan = previousLevelPlan,
            corridorValidation = corridorValidation,
            regenerationAttempt = regenerationAttempt,
            previousOptions = isRegeneration ? previousOptions : null
        };

        string json = JsonUtility.ToJson(requestBody);
        byte[] body = Encoding.UTF8.GetBytes(json);

        using (UnityWebRequest request = new UnityWebRequest(GetBackendUrl(GeneratePath), "POST"))
        {
            request.uploadHandler = new UploadHandlerRaw(body);
            request.downloadHandler = new DownloadHandlerBuffer();
            request.SetRequestHeader("Content-Type", "application/json");
            request.SetRequestHeader("Accept", "application/json");
            request.timeout = Mathf.Max(1, requestTimeoutSeconds);
            yield return request.SendWebRequest();

            if (request.result == UnityWebRequest.Result.Success)
            {
                ApplyResponse(request.downloadHandler.text);
            }
            else
            {
                requestFailed = true;
                options = new HARevisionPlanOption[0];
                SetOptionTexts(null);
                SetStatus("Revision plan request failed. Select Retry to try again.");
                Debug.LogWarning(
                    "HAPlanController request failed: error=" + request.error
                    + ", responseCode=" + request.responseCode
                    + ", responseBody=" + request.downloadHandler.text
                );
            }
        }

        isRequesting = false;
        UpdateButtonState();
    }

    private void ApplyResponse(string json)
    {
        HARevisionPlanResponse response = null;

        try
        {
            response = JsonUtility.FromJson<HARevisionPlanResponse>(json);
        }
        catch (Exception exception)
        {
            Debug.LogWarning("HAPlanController could not parse response: " + exception.Message);
        }

        if (response == null || response.options == null || response.options.Length != 3)
        {
            requestFailed = true;
            options = new HARevisionPlanOption[0];
            SetOptionTexts(null);
            SetStatus("The server returned invalid revision plans. Select Retry.");
            return;
        }

        for (int i = 0; i < response.options.Length; i++)
        {
            HARevisionPlanOption option = response.options[i];

            if (option == null
                || string.IsNullOrWhiteSpace(option.title)
                || string.IsNullOrWhiteSpace(option.description)
                || string.IsNullOrWhiteSpace(option.promptText))
            {
                requestFailed = true;
                options = new HARevisionPlanOption[0];
                SetOptionTexts(null);
                SetStatus("The server returned an incomplete revision plan. Select Retry.");
                return;
            }
        }

        requestFailed = false;
        options = response.options;
        lastSuccessfulOptions = CopyOptions(response.options);
        selectedOptionIndex = -1;
        SetOptionTexts(options);
        SetStatus("Choose one revision plan.");

        if (logPlanEvents)
        {
            Debug.Log(
                "HA revision plans ready: count=" + options.Length
                + ", regenerationAttempt=" + regenerationAttempt
            );
        }
    }

    private void Regenerate()
    {
        if (isRequesting || isContinuing)
        {
            return;
        }

        HARevisionPlanOption[] previousOptions = CopyOptions(lastSuccessfulOptions);

        if (!requestFailed
            && lastSuccessfulOptions != null
            && lastSuccessfulOptions.Length == 3)
        {
            regenerationAttempt += 1;
        }

        StartCoroutine(RequestPlansRoutine(true, previousOptions));
    }

    private void SelectOption(int index)
    {
        if (isRequesting
            || requestFailed
            || options == null
            || index < 0
            || index >= options.Length)
        {
            return;
        }

        selectedOptionIndex = index;
        SetStatus("Selected: " + options[index].title);
        UpdateButtonState();
    }

    private void ConfirmSelection()
    {
        if (isContinuing
            || requestFailed
            || selectedOptionIndex < 0
            || options == null
            || selectedOptionIndex >= options.Length)
        {
            return;
        }

        StartCoroutine(RecordChoiceAndContinueRoutine());
    }

    private IEnumerator RecordChoiceAndContinueRoutine()
    {
        isContinuing = true;
        UpdateButtonState();
        SetStatus("Recording selected revision plan...");

        HARevisionPlanOption selectedOption = options[selectedOptionIndex];
        CreativeWorkshopContext.SetSelectedHAPlan(JsonUtility.ToJson(selectedOption));

        HARevisionPlanChoiceRecord record = new HARevisionPlanChoiceRecord
        {
            eventType = "ha-plan-choice",
            ideaId = GetContextValue(
                CreativeWorkshopContext.IdeaId,
                CreativeWorkshopContext.IdeaIdPrefsKey
            ),
            sessionId = GetContextValue(
                CreativeWorkshopContext.SessionId,
                CreativeWorkshopContext.SessionIdPrefsKey
            ),
            gameRoundId = LevelStudyRecorder.CurrentGameRoundId,
            gameRoundIndex = LevelStudyRecorder.CurrentGameRoundIndex,
            adjustmentText = adjustmentText,
            previousLevelPlan = previousLevelPlan,
            corridorValidation = corridorValidation,
            regenerationAttempt = regenerationAttempt,
            presentedOptions = options,
            selectedOptionId = selectedOption.id,
            selectedOptionTitle = selectedOption.title,
            selectedOptionDescription = selectedOption.description,
            selectedOptionPromptText = selectedOption.promptText,
            sceneName = SceneManager.GetActiveScene().name,
            officialRound = LevelStudyRecorder.IsOfficialRoundFlow,
            timestamp = DateTime.UtcNow.ToString("o")
        };

        yield return PostChoiceRecordRoutine(record);

        if (logPlanEvents)
        {
            Debug.Log(
                "HA revision plan selected: option=" + selectedOption.id
                + ", regenerationAttempt=" + regenerationAttempt
            );
        }

        if (!string.IsNullOrEmpty(nextSceneName))
        {
            SceneManager.LoadScene(nextSceneName);
        }
    }

    private IEnumerator PostChoiceRecordRoutine(HARevisionPlanChoiceRecord record)
    {
        byte[] body = Encoding.UTF8.GetBytes(JsonUtility.ToJson(record));

        using (UnityWebRequest request = new UnityWebRequest(GetBackendUrl(ChoicePath), "POST"))
        {
            request.uploadHandler = new UploadHandlerRaw(body);
            request.downloadHandler = new DownloadHandlerBuffer();
            request.SetRequestHeader("Content-Type", "application/json");
            request.SetRequestHeader("Accept", "application/json");
            request.timeout = Mathf.Max(1, requestTimeoutSeconds);
            yield return request.SendWebRequest();

            if (request.result != UnityWebRequest.Result.Success)
            {
                Debug.LogWarning(
                    "HAPlanController choice record failed: error=" + request.error
                    + ", responseCode=" + request.responseCode
                );
            }
        }
    }

    private void SetOptionTexts(HARevisionPlanOption[] displayOptions)
    {
        for (int i = 0; i < 3; i++)
        {
            bool hasOption = displayOptions != null
                && i < displayOptions.Length
                && displayOptions[i] != null;

            if (optionTitleTexts[i] != null)
            {
                optionTitleTexts[i].text = hasOption
                    ? displayOptions[i].title
                    : "Option " + (i + 1);
            }

            if (optionDescriptionTexts[i] != null)
            {
                optionDescriptionTexts[i].text = hasOption
                    ? displayOptions[i].description
                    : "";
                LayoutRebuilder.ForceRebuildLayoutImmediate(
                    optionDescriptionTexts[i].rectTransform
                );
            }
        }

        Canvas.ForceUpdateCanvases();

        for (int i = 0; i < optionDescriptionScrollRects.Length; i++)
        {
            if (optionDescriptionScrollRects[i] != null)
            {
                optionDescriptionScrollRects[i].verticalNormalizedPosition = 1f;
                optionDescriptionScrollRects[i].StopMovement();
            }
        }

        UpdateOptionVisuals();
    }

    private void UpdateButtonState()
    {
        bool hasOptions = options != null && options.Length == 3 && !requestFailed;

        if (submitButton != null)
        {
            submitButton.interactable = !isRequesting
                && !isContinuing
                && hasOptions
                && selectedOptionIndex >= 0
                && selectedOptionIndex < options.Length;
        }

        if (regenerateButton != null)
        {
            regenerateButton.interactable = !isRequesting && !isContinuing;
        }

        UpdateOptionVisuals();
    }

    private void UpdateOptionVisuals()
    {
        bool hasOptions = options != null && options.Length == 3 && !requestFailed;

        for (int i = 0; i < 3; i++)
        {
            if (optionButtons[i] != null)
            {
                optionButtons[i].interactable = !isRequesting && !isContinuing && hasOptions;
            }

            if (optionCardImages[i] != null)
            {
                optionCardImages[i].color = !hasOptions || isRequesting || isContinuing
                    ? disabledCardColor
                    : i == selectedOptionIndex ? selectedCardColor : normalCardColor;
            }
        }
    }

    private HARevisionPlanOption[] CopyOptions(HARevisionPlanOption[] source)
    {
        if (source == null || source.Length == 0)
        {
            return null;
        }

        HARevisionPlanOption[] copy = new HARevisionPlanOption[Mathf.Min(3, source.Length)];

        for (int i = 0; i < copy.Length; i++)
        {
            HARevisionPlanOption option = source[i];

            if (option != null)
            {
                copy[i] = new HARevisionPlanOption
                {
                    id = option.id,
                    title = option.title,
                    description = option.description,
                    promptText = option.promptText
                };
            }
        }

        return copy;
    }

    private Button FindButton(string objectName, string label)
    {
        GameObject namedObject = GameObject.Find(objectName);

        if (namedObject != null && namedObject.GetComponent<Button>() != null)
        {
            return namedObject.GetComponent<Button>();
        }

        Button[] buttons = FindObjectsOfType<Button>();

        for (int i = 0; i < buttons.Length; i++)
        {
            Text text = buttons[i].GetComponentInChildren<Text>();
            TMP_Text tmpText = buttons[i].GetComponentInChildren<TMP_Text>();

            if ((text != null && CleanText(text.text) == label)
                || (tmpText != null && CleanText(tmpText.text) == label))
            {
                return buttons[i];
            }
        }

        return null;
    }

    private Text FindText(string objectName)
    {
        GameObject textObject = GameObject.Find(objectName);
        return textObject != null ? textObject.GetComponent<Text>() : null;
    }

    private string GetContextValue(string runtimeValue, string prefsKey)
    {
        return !string.IsNullOrEmpty(runtimeValue)
            ? runtimeValue
            : PlayerPrefs.GetString(prefsKey, "");
    }

    private string GetBackendUrl(string path)
    {
        string baseUrl = string.IsNullOrWhiteSpace(backendBaseUrl)
            ? DefaultBackendBaseUrl
            : backendBaseUrl.TrimEnd('/');
        return baseUrl + path;
    }

    private void SetStatus(string message)
    {
        if (runtimeStatusText != null)
        {
            runtimeStatusText.text = message;
        }

        if (statusText != null)
        {
            statusText.text = message;
        }
    }

    private string CleanText(string value)
    {
        return string.IsNullOrEmpty(value)
            ? ""
            : string.Join(" ", value.Trim().Split(
                (char[])null,
                StringSplitOptions.RemoveEmptyEntries
            ));
    }

    [Serializable]
    public class HARevisionPlanRequest
    {
        public string ideaId;
        public string sessionId;
        public string adjustmentText;
        public string sceneName;
        public LevelDesignPlan previousLevelPlan;
        public CorridorValidationResult corridorValidation;
        public int regenerationAttempt;
        public HARevisionPlanOption[] previousOptions;
    }

    [Serializable]
    public class HARevisionPlanResponse
    {
        public HARevisionPlanOption[] options;
    }

    [Serializable]
    public class HARevisionPlanOption
    {
        public string id;
        public string title;
        public string description;
        public string promptText;
    }

    [Serializable]
    public class HARevisionPlanChoiceRecord
    {
        public string eventType;
        public string ideaId;
        public string sessionId;
        public string gameRoundId;
        public int gameRoundIndex;
        public string adjustmentText;
        public LevelDesignPlan previousLevelPlan;
        public CorridorValidationResult corridorValidation;
        public int regenerationAttempt;
        public HARevisionPlanOption[] presentedOptions;
        public string selectedOptionId;
        public string selectedOptionTitle;
        public string selectedOptionDescription;
        public string selectedOptionPromptText;
        public string sceneName;
        public bool officialRound;
        public string timestamp;
    }
}
