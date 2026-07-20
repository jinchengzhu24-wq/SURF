using UnityEngine;
using UnityEngine.SceneManagement;

public class MenuController : MonoBehaviour
{
    public string targetSceneName = "Algorithm_Level";
    public string creativeWorkshopSceneName = "Questionnaire(Before)";

    public void StartGame()
    {
        LevelStudyRecorder.MarkMenuStartedFlow();
        LoadScene(creativeWorkshopSceneName, "creative workshop");
    }

    public void TryGame()
    {
        if (string.IsNullOrEmpty(targetSceneName))
        {
            Debug.LogWarning("MenuController: Target scene name is empty.");
            return;
        }

        LevelStudyRecorder.BeginGameRound();
        SceneManager.LoadScene(targetSceneName);
    }

    private void LoadScene(string sceneName, string sceneLabel)
    {
        if (string.IsNullOrEmpty(sceneName))
        {
            Debug.LogWarning("MenuController: " + sceneLabel + " scene name is empty.");
            return;
        }

        SceneManager.LoadScene(sceneName);
    }

    public void QuitGame()
    {
        Debug.Log("MenuController: Quit game requested.");

#if UNITY_EDITOR
        UnityEditor.EditorApplication.isPlaying = false;
#else
        Application.Quit();
#endif
    }
}
