using System;
using System.Collections;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

// The DG scene owns every visible control. This controller binds only serialized references.
public sealed class DescriptionGenerationController : MonoBehaviour
{
    private const string GuideEndpoint = "http://111.231.136.4:8000/dg/guide/summary";
    private static readonly string[] RelationshipValues = { "friend", "acquaintance", "stranger" };
    private static readonly string[] ExperienceValues = { "relaxed", "challenging_fair", "breakthrough" };
    private static readonly string[] DifficultyLabels = { "Easy", "Medium", "Hard", "Random" };
    private static readonly Preset[] Presets =
    {
        new Preset("Easy", 18, 32, 8, 14, 14, 24),
        new Preset("Medium", 22, 42, 10, 22, 18, 34),
        new Preset("Hard", 30, 50, 16, 28, 24, 40)
    };

    [Header("Navigation")]
    public string nextSceneName = "DG_Level";
    [Header("Request")]
    [SerializeField] private bool restoreSavedSettings;
    [SerializeField] private string guideEndpoint = GuideEndpoint;
    [SerializeField] private int requestTimeoutSeconds = 15;
    [Header("Static Scene References")]
    [SerializeField] private Text progressText;
    [SerializeField] private Text summaryText;
    [SerializeField] private Text rationaleText;
    [SerializeField] private Text statusText;
    [SerializeField] private Text difficultyText;
    [SerializeField] private GameObject difficultyControls;
    [SerializeField] private Text questionText;
    [SerializeField] private Button[] optionButtons;
    [Header("Static Question Copy")]
    [TextArea(2, 3)] [SerializeField] private string relationshipQuestion = "What is your relationship with the opponent?";
    [SerializeField] private string[] relationshipOptions = { "Close friend", "Acquaintance", "Stranger" };
    [TextArea(2, 3)] [SerializeField] private string experienceQuestion = "What should the opponent feel after playing the level?";
    [SerializeField] private string[] experienceOptions = { "Relaxed and approachable", "Challenging but fair", "Several attempts and a breakthrough" };
    [SerializeField] private Button previousDifficultyButton;
    [SerializeField] private Button nextDifficultyButton;
    [SerializeField] private Button confirmButton;

    private DescriptionGenerationSettings settings;
    private int displayedDifficulty = 3;
    private int questionIndex;
    private bool summaryReady;
    private bool requestInFlight;

    private sealed class Preset
    {
        public readonly string label;
        private readonly int minSteps, maxSteps, minPushes, maxPushes, minPulls, maxPulls;
        public Preset(string label, int minSteps, int maxSteps, int minPushes, int maxPushes, int minPulls, int maxPulls)
        {
            this.label = label; this.minSteps = minSteps; this.maxSteps = maxSteps;
            this.minPushes = minPushes; this.maxPushes = maxPushes; this.minPulls = minPulls; this.maxPulls = maxPulls;
        }
        public void Apply(LevelGenerationPreferences target)
        {
            target.minSolutionSteps = minSteps; target.maxSolutionSteps = maxSteps;
            target.minPushes = minPushes; target.maxPushes = maxPushes;
            target.minReversePulls = minPulls; target.maxReversePulls = maxPulls;
        }
        public bool Matches(LevelGenerationPreferences value) => value.minSolutionSteps == minSteps && value.maxSolutionSteps == maxSteps && value.minPushes == minPushes && value.maxPushes == maxPushes && value.minReversePulls == minPulls && value.maxReversePulls == maxPulls;
    }

    private void Start()
    {
        if (!HasValidStaticInterface())
        {
            Debug.LogError("DescriptionGenerationController: assign every static DG scene reference in the Inspector.");
            enabled = false;
            return;
        }
        DescriptionGenerationSettings saved = null;
        bool loaded = restoreSavedSettings && DescriptionGenerationContext.TryLoad(out saved);
        settings = loaded && saved != null ? saved : NewSettings(1);
        displayedDifficulty = loaded ? ResolveSavedDifficulty() : 3;
        BindStaticControls();
        ShowQuestion(0);
    }

    private bool HasValidStaticInterface() => questionText != null && summaryText != null && rationaleText != null && difficultyText != null && difficultyControls != null && previousDifficultyButton != null && nextDifficultyButton != null && confirmButton != null && HasThreeButtons(optionButtons) && HasThreeLabels(relationshipOptions) && HasThreeLabels(experienceOptions);
    private static bool HasThreeButtons(Button[] buttons) => buttons != null && buttons.Length == 3 && buttons[0] != null && buttons[1] != null && buttons[2] != null;
    private static bool HasThreeLabels(string[] labels) => labels != null && labels.Length == 3 && !string.IsNullOrWhiteSpace(labels[0]) && !string.IsNullOrWhiteSpace(labels[1]) && !string.IsNullOrWhiteSpace(labels[2]);

    private void BindStaticControls()
    {
        previousDifficultyButton.onClick.RemoveAllListeners(); previousDifficultyButton.onClick.AddListener(() => ChangeDifficulty(-1));
        nextDifficultyButton.onClick.RemoveAllListeners(); nextDifficultyButton.onClick.AddListener(() => ChangeDifficulty(1));
        confirmButton.onClick.RemoveAllListeners(); confirmButton.onClick.AddListener(SaveAndContinue);
        UpdateDifficultyUi(); SetControlsInteractable(false);
    }

    private void ShowQuestion(int index)
    {
        questionIndex = index;
        if (progressText != null) progressText.text = "Question " + (index + 1) + " of 2";
        questionText.text = index == 0 ? relationshipQuestion : experienceQuestion;
        if (statusText != null) statusText.text = "Choose one option.";
        string[] labels = index == 0 ? relationshipOptions : experienceOptions;
        for (int i = 0; i < optionButtons.Length; i++)
        {
            int answerIndex = i;
            SetButtonLabel(optionButtons[i], labels[i]);
            optionButtons[i].onClick.RemoveAllListeners(); optionButtons[i].onClick.AddListener(() => SelectAnswer(answerIndex));
            optionButtons[i].interactable = true;
        }
        SetControlsInteractable(false);
    }

    private void SelectAnswer(int index)
    {
        if (questionIndex == 0) { settings.opponentRelationship = RelationshipValues[index]; ShowQuestion(1); return; }
        settings.opponentExperience = ExperienceValues[index]; StartCoroutine(RequestSummary());
    }

    private IEnumerator RequestSummary()
    {
        requestInFlight = true;
        SetOptionButtonsInteractable(false);
        summaryText.text = "AI reflection is being prepared...";
        rationaleText.text = "AI difficulty suggestion is being prepared...";
        if (statusText != null) statusText.text = "AI is preparing a summary and difficulty suggestion...";
        GuideRequest payload = new GuideRequest { opponentRelationship = settings.opponentRelationship, opponentExperience = settings.opponentExperience, language = "en" };
        string endpoint = string.IsNullOrWhiteSpace(guideEndpoint) ? GuideEndpoint : guideEndpoint.Trim();
        using (UnityWebRequest request = new UnityWebRequest(endpoint, "POST"))
        {
            request.uploadHandler = new UploadHandlerRaw(Encoding.UTF8.GetBytes(JsonUtility.ToJson(payload)));
            request.downloadHandler = new DownloadHandlerBuffer(); request.SetRequestHeader("Content-Type", "application/json");
            request.timeout = Mathf.Max(1, requestTimeoutSeconds); yield return request.SendWebRequest();
            if (request.result == UnityWebRequest.Result.Success)
            {
                GuideResponse response = null;
                try { response = JsonUtility.FromJson<GuideResponse>(request.downloadHandler.text); }
                catch (Exception exception) { Debug.LogWarning("DG guide response parse failed: " + exception.Message); }
                if (response != null && IsValidDifficulty(response.recommendedDifficulty) && !string.IsNullOrWhiteSpace(response.summary) && !string.IsNullOrWhiteSpace(response.rationale))
                { ApplyGuideResponse(response); requestInFlight = false; yield break; }
            }
            // The deployed 8000 service may temporarily predate this optional guide route.
            // Continue with a deterministic, player-readable result instead of surfacing a
            // transport warning in the Unity console.
        }
        requestInFlight = false;
        ApplyGuideResponse(BuildFallbackResponse());
    }

    private GuideResponse BuildFallbackResponse()
    {
        string difficulty = settings.opponentExperience == "relaxed" ? "Easy" : settings.opponentExperience == "breakthrough" ? "Hard" : "Medium";
        string summary = settings.opponentExperience == "relaxed"
            ? "I get the sense that you may want a calm opening where the other player can settle into the puzzle and feel capable early on."
            : settings.opponentExperience == "breakthrough"
                ? "I get the sense that you may be aiming for a patient struggle that turns into a satisfying moment of recognition rather than a punishing surprise."
                : "I get the sense that you may want a few decisions to create real pressure, while still letting the other player understand why each solution works.";
        string tone = settings.opponentRelationship == "friend" ? "a caring shared challenge" : settings.opponentRelationship == "acquaintance" ? "a clear and welcoming challenge" : "a readable challenge that earns trust quickly";
        return new GuideResponse { summary = summary, rationale = "I would begin with " + difficulty + " because it best supports " + tone + " without treating the relationship itself as a difficulty setting.", recommendedDifficulty = difficulty, source = "deterministic_fallback" };
    }

    private void ApplyGuideResponse(GuideResponse response)
    {
        // The completed state must never retain question controls, including when a
        // response arrives after a UI event has refreshed the question panel.
        settings.aiSummary = response.summary.Trim(); settings.aiRationale = response.rationale.Trim();
        settings.aiRecommendedDifficulty = response.recommendedDifficulty; settings.aiRecommendationSource = response.source ?? "llm";
        displayedDifficulty = DifficultyIndex(settings.aiRecommendedDifficulty);
        summaryText.text = "AI summary: " + settings.aiSummary;
        rationaleText.text = "AI suggestion: " + DifficultyLabels[displayedDifficulty] + "\n" + settings.aiRationale;
        if (statusText != null) statusText.text = "Review the summary, adjust difficulty if needed, then confirm.";
        summaryReady = true; UpdateDifficultyUi(); SetControlsInteractable(true);
    }

    private void ChangeDifficulty(int delta)
    {
        displayedDifficulty = (displayedDifficulty + delta + DifficultyLabels.Length) % DifficultyLabels.Length;
        if (displayedDifficulty < Presets.Length) Presets[displayedDifficulty].Apply(settings.preferences);
        settings.finalDifficulty = DifficultyLabels[displayedDifficulty]; UpdateDifficultyUi();
    }

    private void UpdateDifficultyUi() => difficultyText.text = DifficultyLabels[displayedDifficulty];
    private void SetControlsInteractable(bool interactable)
    {
        previousDifficultyButton.interactable = interactable; nextDifficultyButton.interactable = interactable;
        confirmButton.interactable = interactable && summaryReady && !requestInFlight;
    }
    private void SetOptionButtonsInteractable(bool interactable)
    {
        foreach (Button button in optionButtons) button.interactable = interactable;
    }
    private static DescriptionGenerationSettings NewSettings(int presetIndex) { DescriptionGenerationSettings result = new DescriptionGenerationSettings(); Presets[presetIndex].Apply(result.preferences); return result; }
    private int ResolveSavedDifficulty() { for (int i = 0; i < Presets.Length; i++) if (Presets[i].Matches(settings.preferences)) return i; return 3; }
    private void SaveAndContinue()
    {
        if (!summaryReady || requestInFlight) return;
        int selected = displayedDifficulty == 3 ? UnityEngine.Random.Range(0, Presets.Length) : displayedDifficulty;
        Presets[selected].Apply(settings.preferences); settings.finalDifficulty = Presets[selected].label;
        DescriptionGenerationContext.Save(settings);
        if (!string.IsNullOrWhiteSpace(nextSceneName) && Application.CanStreamedLevelBeLoaded(nextSceneName)) { SceneManager.LoadScene(nextSceneName); return; }
        if (statusText != null) statusText.text = "The DG result scene is unavailable.";
    }
    private static int DifficultyIndex(string value) => string.Equals(value, "Easy", StringComparison.OrdinalIgnoreCase) ? 0 : string.Equals(value, "Medium", StringComparison.OrdinalIgnoreCase) ? 1 : string.Equals(value, "Hard", StringComparison.OrdinalIgnoreCase) ? 2 : 3;
    private static bool IsValidDifficulty(string value) => value == "Easy" || value == "Medium" || value == "Hard" || value == "Random";
    private static void SetButtonLabel(Button button, string value) { Text label = button.GetComponentInChildren<Text>(true); if (label != null) label.text = value; }
    [Serializable] private sealed class GuideRequest { public string opponentRelationship; public string opponentExperience; public string language; }
    [Serializable] private sealed class GuideResponse { public string summary; public string rationale; public string recommendedDifficulty; public string source; }
}
