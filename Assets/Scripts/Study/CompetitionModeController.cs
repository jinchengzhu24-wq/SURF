using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class CompetitionModeController : MonoBehaviour
{
    public const string SelectedModePrefsKey = "SokobanMatchmakingCompetitionMode";
    public const string CompetitiveModeId = "competitive";
    public const string SupportiveModeId = "supportive";

    [Header("Navigation")]
    public string nextSceneName = "AI_Asistant_Mode";

    [Header("Scene UI")]
    public Button confirmButton;

    private QuestionnaireOptionButton[] optionButtons =
        new QuestionnaireOptionButton[0];
    private QuestionnaireOptionButton selectedOption;

    public static string GetSelectedMode()
    {
        string selectedMode = PlayerPrefs.GetString(
            SelectedModePrefsKey,
            CompetitiveModeId
        );

        return IsValidMode(selectedMode)
            ? selectedMode
            : CompetitiveModeId;
    }

    public static bool IsValidMode(string mode)
    {
        return mode == CompetitiveModeId || mode == SupportiveModeId;
    }

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
            Debug.LogWarning("CompetitionModeController: No competition mode is selected.");
            return;
        }

        PlayerPrefs.SetString(SelectedModePrefsKey, selectedOption.optionId);
        PlayerPrefs.Save();

        if (string.IsNullOrWhiteSpace(nextSceneName))
        {
            Debug.LogWarning("CompetitionModeController: Next scene name is empty.");
            return;
        }

        SceneManager.LoadScene(nextSceneName);
    }
}
