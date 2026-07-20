using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class RefinementController : MonoBehaviour
{
    [Header("Scene UI")]
    public Button submitButton;
    public Button detailButton;
    public Text statusText;

    [Header("Scoring")]
    [Min(1)]
    public int requiredQuestionCount = 3;
    public int passScoreThreshold = 6;

    [Header("Flow")]
    public string questionnaireSceneName = "Questionnaire(After)";
    public string retrySceneName = "Adjustment";
    public string designInterpretationSceneName = "Design interpretation";

    [Header("Retry Prompt")]
    public string retryPrompt =
        "Refinement feedback - the previous level was not satisfactory; generate a stricter version that follows the selected direction and original idea more closely.";
    public bool logRefinementEvents = true;

    private readonly Dictionary<int, QuestionnaireOptionButton> selectedOptions =
        new Dictionary<int, QuestionnaireOptionButton>();

    private QuestionnaireOptionButton[] optionButtons = new QuestionnaireOptionButton[0];

    private void Start()
    {
        ResolveSceneReferences();
        WireButtons();
        RefreshSelectionVisuals();
        SetStatus("");
        UpdateSubmitState();
    }

    private void ResolveSceneReferences()
    {
        optionButtons = FindObjectsOfType<QuestionnaireOptionButton>();

        if (submitButton == null)
        {
            GameObject submitObject = GameObject.Find("SubmitButton");

            if (submitObject != null)
            {
                submitButton = submitObject.GetComponent<Button>();
            }
        }

        if (detailButton == null)
        {
            GameObject detailObject = GameObject.Find("DetailButton");

            if (detailObject != null)
            {
                detailButton = detailObject.GetComponent<Button>();
            }
        }

        if (detailButton == null)
        {
            detailButton = FindButtonByText("Detail");
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

    private void WireButtons()
    {
        for (int i = 0; i < optionButtons.Length; i++)
        {
            QuestionnaireOptionButton option = optionButtons[i];

            if (option == null)
            {
                continue;
            }

            option.ResolveReferences();
            Button button = option.Button;

            if (button == null)
            {
                continue;
            }

            button.onClick.RemoveAllListeners();
            button.onClick.AddListener(() => SelectOption(option));
        }

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
    }

    private void SelectOption(QuestionnaireOptionButton selectedOption)
    {
        if (selectedOption == null)
        {
            return;
        }

        selectedOptions[selectedOption.questionIndex] = selectedOption;
        RefreshSelectionVisuals();
        UpdateSubmitState();
    }

    private void RefreshSelectionVisuals()
    {
        for (int i = 0; i < optionButtons.Length; i++)
        {
            QuestionnaireOptionButton option = optionButtons[i];

            if (option == null)
            {
                continue;
            }

            QuestionnaireOptionButton selectedOption;
            bool selected = selectedOptions.TryGetValue(option.questionIndex, out selectedOption)
                && selectedOption.optionIndex == option.optionIndex;
            option.SetSelected(selected);
        }
    }

    private void UpdateSubmitState()
    {
        if (submitButton != null)
        {
            submitButton.interactable = IsComplete();
        }
    }

    private bool IsComplete()
    {
        List<int> requiredIndexes = GetRequiredQuestionIndexes();

        if (requiredIndexes.Count < Mathf.Max(1, requiredQuestionCount))
        {
            return false;
        }

        for (int i = 0; i < requiredIndexes.Count; i++)
        {
            if (!selectedOptions.ContainsKey(requiredIndexes[i]))
            {
                return false;
            }
        }

        return true;
    }

    private void Submit()
    {
        if (!IsComplete())
        {
            return;
        }

        int totalScore = GetTotalScore();

        if (logRefinementEvents)
        {
            Debug.Log(
                "Refinement submitted:"
                + " totalScore=" + totalScore
                + ", passScoreThreshold=" + passScoreThreshold
            );
        }

        if (totalScore >= passScoreThreshold)
        {
            LoadScene(questionnaireSceneName);
            return;
        }

        ApplyRetryPrompt(totalScore);
        LoadScene(retrySceneName);
    }

    private int GetTotalScore()
    {
        int totalScore = 0;
        List<int> requiredIndexes = GetRequiredQuestionIndexes();

        for (int i = 0; i < requiredIndexes.Count; i++)
        {
            QuestionnaireOptionButton option;

            if (!selectedOptions.TryGetValue(requiredIndexes[i], out option) || option == null)
            {
                continue;
            }

            totalScore += Mathf.Clamp(option.optionIndex + 1, 1, 3);
        }

        return totalScore;
    }

    private List<int> GetRequiredQuestionIndexes()
    {
        HashSet<int> indexSet = new HashSet<int>();

        for (int i = 0; i < optionButtons.Length; i++)
        {
            QuestionnaireOptionButton option = optionButtons[i];

            if (option != null)
            {
                indexSet.Add(option.questionIndex);
            }
        }

        List<int> indexes = new List<int>(indexSet);
        indexes.Sort();

        int count = Mathf.Min(indexes.Count, Mathf.Max(1, requiredQuestionCount));

        if (indexes.Count > count)
        {
            indexes.RemoveRange(count, indexes.Count - count);
        }

        return indexes;
    }

    private void ApplyRetryPrompt(int totalScore)
    {
        string ideaId = GetCreativeIdeaId();
        string sessionId = GetCreativeSessionId();
        string ideaText = GetCreativeIdeaText();
        string refinedIdeaText = BuildRefinedIdeaText(ideaText, totalScore);

        CreativeWorkshopContext.SetRefinementFeedback(
            ideaId,
            sessionId,
            BuildRefinementFeedback(totalScore),
            refinedIdeaText
        );
        LevelStudyRecorder.UpdateCustomRoundIdea(ideaId, refinedIdeaText);

        if (logRefinementEvents)
        {
            Debug.Log(
                "Refinement requested retry:"
                + " totalScore=" + totalScore
                + ", ideaId=" + ideaId
            );
        }
    }

    private string BuildRefinedIdeaText(string ideaText, int totalScore)
    {
        string cleanIdeaText = CleanText(ideaText);
        string feedback = BuildRefinementFeedback(totalScore);

        if (string.IsNullOrEmpty(cleanIdeaText))
        {
            return feedback;
        }

        return cleanIdeaText + " " + feedback;
    }

    private string BuildRefinementFeedback(int totalScore)
    {
        return "Refinement score " + totalScore + ". " + CleanText(retryPrompt);
    }

    private string GetCreativeIdeaId()
    {
        return !string.IsNullOrEmpty(CreativeWorkshopContext.IdeaId)
            ? CreativeWorkshopContext.IdeaId
            : PlayerPrefs.GetString(CreativeWorkshopContext.IdeaIdPrefsKey, "");
    }

    private string GetCreativeSessionId()
    {
        return !string.IsNullOrEmpty(CreativeWorkshopContext.SessionId)
            ? CreativeWorkshopContext.SessionId
            : PlayerPrefs.GetString(CreativeWorkshopContext.SessionIdPrefsKey, "");
    }

    private string GetCreativeIdeaText()
    {
        return !string.IsNullOrEmpty(CreativeWorkshopContext.IdeaText)
            ? CreativeWorkshopContext.IdeaText
            : PlayerPrefs.GetString(CreativeWorkshopContext.IdeaTextPrefsKey, "");
    }

    private Button FindButtonByText(string text)
    {
        Button[] buttons = FindObjectsOfType<Button>();

        for (int i = 0; i < buttons.Length; i++)
        {
            Button button = buttons[i];

            if (button == null)
            {
                continue;
            }

            Text buttonText = button.GetComponentInChildren<Text>();

            if (buttonText != null && CleanText(buttonText.text) == text)
            {
                return button;
            }
        }

        return null;
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
            Debug.LogWarning("RefinementController: scene name is empty.");
            return;
        }

        SceneManager.LoadScene(sceneName);
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
}
