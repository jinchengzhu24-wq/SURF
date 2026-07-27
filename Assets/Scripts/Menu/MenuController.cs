using UnityEngine;
using UnityEngine.SceneManagement;

public class MenuController : MonoBehaviour
{
    public string targetSceneName = "Algorithm_Level";
    public string creativeWorkshopSceneName = "Questionnaire(Before)";
    public string studyDashboardUrl = "http://111.231.136.4:8000/frontend/";

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

    public void OpenStudyDashboard()
    {
        string dashboardUrl = studyDashboardUrl?.Trim();

        if (string.IsNullOrEmpty(dashboardUrl))
        {
            Debug.LogWarning("MenuController: Study dashboard URL is empty.");
            return;
        }

        if (!System.Uri.TryCreate(dashboardUrl, System.UriKind.Absolute, out System.Uri uri)
            || (uri.Scheme != System.Uri.UriSchemeHttp
                && uri.Scheme != System.Uri.UriSchemeHttps))
        {
            Debug.LogWarning("MenuController: Study dashboard URL is invalid: " + dashboardUrl);
            return;
        }

        Debug.Log("MenuController: Opening study dashboard: " + uri.AbsoluteUri);
        Application.OpenURL(uri.AbsoluteUri);
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
