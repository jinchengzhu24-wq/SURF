using System;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class AIAssistantModeController : MonoBehaviour
{
    public const string SelectedModePrefsKey =
        "SokobanMatchmakingAIAssistantMode";

    [Header("Navigation")]
    public string descriptionGenerationSceneName = "DG";
    public string partialCompletionSceneName = "PC";

    [Header("Scene UI")]
    public Button confirmButton;

    private QuestionnaireOptionButton[] optionButtons =
        new QuestionnaireOptionButton[0];
    private QuestionnaireOptionButton selectedOption;

    private void Start()
    {
        ResolveSceneReferences();
        WireButtons();
        UpdateConfirmState();
    }

    private void ResolveSceneReferences()
    {
        optionButtons = FindObjectsOfType<QuestionnaireOptionButton>();

        if (confirmButton == null)
        {
            GameObject confirmObject = GameObject.Find("ConfirmButton");

            if (confirmObject != null)
            {
                confirmButton = confirmObject.GetComponent<Button>();
            }
        }
    }

    private void WireButtons()
    {
        for (int i = 0; i < optionButtons.Length; i++)
        {
            QuestionnaireOptionButton option = optionButtons[i];
            option.ResolveReferences();
            option.Button.onClick.AddListener(() => SelectOption(option));
            option.SetSelected(false);
        }

        if (confirmButton != null)
        {
            confirmButton.onClick.AddListener(ConfirmSelection);
        }
    }

    private void SelectOption(QuestionnaireOptionButton option)
    {
        selectedOption = option;

        for (int i = 0; i < optionButtons.Length; i++)
        {
            optionButtons[i].SetSelected(optionButtons[i] == selectedOption);
        }

        UpdateConfirmState();
    }

    private void UpdateConfirmState()
    {
        if (confirmButton != null)
        {
            confirmButton.interactable = selectedOption != null;
        }
    }

    private void ConfirmSelection()
    {
        if (selectedOption == null)
        {
            Debug.LogWarning(
                "AIAssistantModeController: No assistant mode is selected."
            );
            return;
        }

        string optionId = (selectedOption.optionId ?? "").Trim();
        string targetSceneName = ResolveTargetScene(optionId);

        if (string.IsNullOrWhiteSpace(targetSceneName))
        {
            Debug.LogWarning(
                "AIAssistantModeController: No scene is configured for option "
                + optionId
                + "."
            );
            return;
        }

        PlayerPrefs.SetString(SelectedModePrefsKey, optionId);
        PlayerPrefs.Save();
        SceneManager.LoadScene(targetSceneName);
    }

    private string ResolveTargetScene(string optionId)
    {
        if (string.Equals(optionId, "a", StringComparison.OrdinalIgnoreCase))
        {
            return descriptionGenerationSceneName;
        }

        if (string.Equals(optionId, "b", StringComparison.OrdinalIgnoreCase))
        {
            return partialCompletionSceneName;
        }

        return "";
    }
}
