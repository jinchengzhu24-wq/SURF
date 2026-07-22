using System.Collections.Generic;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class RoutingController : MonoBehaviour
{
    private const int AiOptionIndex = 0;
    private const int HumanOptionIndex = 1;
    private const int HaOptionIndex = 2;

    [Header("Scene UI")]
    public Button confirmButton;

    [Header("Target Scenes")]
    public string aiSceneName = "Adjustment(AI)";
    public string humanSceneName = "Adjustment(Human)";
    public string haSceneName = "Adjustment(HA)";

    private QuestionnaireOptionButton[] optionButtons = new QuestionnaireOptionButton[0];
    private int selectedOptionIndex = -1;
    private bool configurationValid;

    private void Start()
    {
        ResolveSceneReferences();
        configurationValid = ValidateOptions();
        WireButtons();
        RefreshSelectionVisuals();
        UpdateConfirmState();
    }

    private void ResolveSceneReferences()
    {
        optionButtons = FindObjectsOfType<QuestionnaireOptionButton>();

        if (confirmButton != null)
        {
            return;
        }

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
