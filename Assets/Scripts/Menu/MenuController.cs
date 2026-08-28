using UnityEngine;
using UnityEngine.SceneManagement;

public class MenuController : MonoBehaviour
{
    public string targetSceneName = "Algorithm_Level";
    public string creativeWorkshopSceneName = "Questionnaire(Before)";
    public string matchmakingSceneName = "Online_Lobby";
    public string tutorialUrl = "http://111.231.136.4/frontend/tutorial/Sokoban_Tutorial_Bilingual.pdf";

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

    public void OpenMatchmaking()
    {
        LevelStudyRecorder.MarkMenuStartedFlow();
        LoadScene(matchmakingSceneName, "matchmaking");
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

    public void OpenTutorial()
    {
        if (TryGetHttpUrl(tutorialUrl, "Tutorial", out string resolvedTutorialUrl))
        {
            Debug.Log("MenuController: Opening tutorial: " + resolvedTutorialUrl);
            Application.OpenURL(resolvedTutorialUrl);
        }
    }

    private bool TryGetHttpUrl(string configuredUrl, string label, out string resolvedUrl)
    {
        resolvedUrl = configuredUrl?.Trim();

        if (string.IsNullOrEmpty(resolvedUrl))
        {
            Debug.LogWarning("MenuController: " + label + " URL is empty.");
            return false;
        }

        if (!System.Uri.TryCreate(resolvedUrl, System.UriKind.Absolute, out System.Uri uri)
            || (uri.Scheme != System.Uri.UriSchemeHttp
                && uri.Scheme != System.Uri.UriSchemeHttps))
        {
            Debug.LogWarning("MenuController: " + label + " URL is invalid: " + resolvedUrl);
            return false;
        }

        resolvedUrl = uri.AbsoluteUri;
        return true;
    }
}
