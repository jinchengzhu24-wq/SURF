using System.Collections.Generic;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class RoutingController : MonoBehaviour
{
    private const int AiOptionIndex = 0;
    private const int HumanOptionIndex = 1;
    private const int HaOptionIndex = 2;
    private const string NavigationUrl =
        "http://111.231.136.4:8000/frontend/Images/Routing.png";

    [Header("Scene UI")]
    public Button confirmButton;
    public Button navigationButton;

    [Header("Target Scenes")]
    public string aiSceneName = "Adjustment(AI)";
    public string humanSceneName = "Adjustment(Human)";
    public string haSceneName = "Adjustment(HA)";

    [Header("First Visit Option Text")]
    [TextArea(2, 4)]
    public string initialAiOptionText = "Let AI decide how to improve the level";
    [TextArea(2, 4)]
    public string initialHumanOptionText = "I will specify exactly what to change";
    [TextArea(2, 4)]
    public string initialHaOptionText = "Let AI suggest plans, then I will choose";

    private QuestionnaireOptionButton[] optionButtons = new QuestionnaireOptionButton[0];
    private int selectedOptionIndex = -1;
    private bool configurationValid;

    private void Start()
    {
        ResolveSceneReferences();
        ApplyInitialOptionTextOverrides();
        configurationValid = ValidateOptions();
        WireButtons();
        RefreshSelectionVisuals();
        UpdateConfirmState();
    }

    private void ResolveSceneReferences()
    {
        optionButtons = FindObjectsOfType<QuestionnaireOptionButton>();

        if (confirmButton == null)
        {
            GameObject confirmObject = GameObject.Find("ConfirmButton");

            if (confirmObject == null)
            {
                confirmObject = GameObject.Find("SubmitButton");
            }

            if (confirmObject != null)
            {
                confirmButton = confirmObject.GetComponent<Button>();
            }
        }

        if (navigationButton == null)
        {
            GameObject navigationObject = GameObject.Find("NavigationButton");

            if (navigationObject != null)
            {
                navigationButton = navigationObject.GetComponent<Button>();
            }
        }
    }

    private void ApplyInitialOptionTextOverrides()
    {
        if (!CreativeWorkshopContext.ShouldOverrideInitialRoutingOptionText)
        {
            return;
        }

        for (int i = 0; i < optionButtons.Length; i++)
        {
            QuestionnaireOptionButton option = optionButtons[i];

            if (option == null)
            {
                continue;
            }

            option.ResolveReferences();
            string overrideText = GetInitialOptionText(option.optionIndex);

            if (option.optionText != null && !string.IsNullOrWhiteSpace(overrideText))
            {
                option.optionText.text = overrideText.Trim();
            }
        }

        CreativeWorkshopContext.ConsumeInitialRoutingOptionTextPending();
    }

    private string GetInitialOptionText(int optionIndex)
    {
        if (optionIndex == AiOptionIndex)
        {
            return initialAiOptionText;
        }

        if (optionIndex == HumanOptionIndex)
        {
            return initialHumanOptionText;
        }

        if (optionIndex == HaOptionIndex)
        {
            return initialHaOptionText;
        }

        return "";
    }

    private bool ValidateOptions()
    {
        HashSet<int> indexes = new HashSet<int>();

        for (int i = 0; i < optionButtons.Length; i++)
        {
            QuestionnaireOptionButton option = optionButtons[i];

            if (option == null)
            {
                continue;
            }

            if (option.optionIndex < AiOptionIndex || option.optionIndex > HaOptionIndex)
            {
                Debug.LogWarning("RoutingController: Unsupported option index " + option.optionIndex + ".");
                return false;
            }

            if (!indexes.Add(option.optionIndex))
            {
                Debug.LogWarning("RoutingController: Duplicate option index " + option.optionIndex + ".");
                return false;
            }
        }

        bool valid = indexes.Contains(AiOptionIndex)
            && indexes.Contains(HumanOptionIndex)
            && indexes.Contains(HaOptionIndex);

        if (!valid)
        {
            Debug.LogWarning("RoutingController: AI, Human, and HA options are all required.");
        }

        return valid;
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

        if (confirmButton != null)
        {
            confirmButton.onClick.RemoveAllListeners();
            confirmButton.onClick.AddListener(ConfirmSelection);
        }
        else
        {
            Debug.LogWarning("RoutingController: Confirm button is missing.");
        }

        if (navigationButton != null)
        {
            navigationButton.onClick.RemoveAllListeners();
            navigationButton.onClick.AddListener(OpenNavigation);
            navigationButton.interactable = true;
        }
        else
        {
            Debug.LogWarning("RoutingController: Navigation button is missing.");
        }
    }

    private void OpenNavigation()
    {
        Application.OpenURL(NavigationUrl);
    }

    private void SelectOption(QuestionnaireOptionButton option)
    {
        if (!configurationValid || option == null)
        {
            return;
        }

        selectedOptionIndex = option.optionIndex;
        RefreshSelectionVisuals();
        UpdateConfirmState();
    }

    private void RefreshSelectionVisuals()
    {
        for (int i = 0; i < optionButtons.Length; i++)
        {
            QuestionnaireOptionButton option = optionButtons[i];

            if (option != null)
            {
                option.SetSelected(option.optionIndex == selectedOptionIndex);
            }
        }
    }

    private void UpdateConfirmState()
    {
        if (confirmButton != null)
        {
            confirmButton.interactable = configurationValid && selectedOptionIndex >= AiOptionIndex;
        }
    }

    private void ConfirmSelection()
    {
        string sceneName = GetSelectedSceneName();

        if (string.IsNullOrEmpty(sceneName))
        {
            Debug.LogWarning("RoutingController: No valid routing option is selected.");
            return;
        }

        string revisionMode = selectedOptionIndex == AiOptionIndex
            ? "ai"
            : selectedOptionIndex == HumanOptionIndex
                ? "human"
                : "ha";
        LevelStudyRecorder.RecordJourneyStage(
            "routing",
            "selected",
            "Selected " + revisionMode.ToUpperInvariant() + " revision route",
            revisionMode
        );
        SceneManager.LoadScene(sceneName);
    }

    private string GetSelectedSceneName()
    {
        if (selectedOptionIndex == AiOptionIndex)
        {
            return aiSceneName;
        }

        if (selectedOptionIndex == HumanOptionIndex)
        {
            return humanSceneName;
        }

        if (selectedOptionIndex == HaOptionIndex)
        {
            return haSceneName;
        }

        return "";
    }
}
