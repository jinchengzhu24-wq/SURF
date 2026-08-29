using System.Collections;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class MenuController : MonoBehaviour
{
    public string targetSceneName = "Algorithm_Level";
    public string creativeWorkshopSceneName = "Questionnaire(Before)";
    public string matchmakingSceneName = "Online_Lobby";
    public string tutorialUrl = "http://111.231.136.4/frontend/tutorial/Sokoban_Tutorial_Bilingual.pdf";
    public Image tryTransitionPanel;
    public float tryTransitionFadeTime = 1f;

    private bool tryTransitioning;

    private void Start()
    {
        SetTryTransitionPanelAlpha(0f);

        if (tryTransitionPanel != null)
        {
            tryTransitionPanel.raycastTarget = false;
        }
    }

    public void StartGame()
    {
        LevelStudyRecorder.MarkMenuStartedFlow();
        LoadScene(creativeWorkshopSceneName, "creative workshop");
    }

    public void TryGame()
    {
        if (tryTransitioning || string.IsNullOrEmpty(targetSceneName))
        {
            if (string.IsNullOrEmpty(targetSceneName))
            {
                Debug.LogWarning("MenuController: Target scene name is empty.");
            }

            return;
        }

        tryTransitioning = true;
        LevelStudyRecorder.BeginGameRound();

        if (tryTransitionPanel == null)
        {
            Debug.LogWarning(
                "MenuController: Try transition panel is missing; loading without a fade."
            );
            SceneManager.LoadScene(targetSceneName);
            return;
        }

        tryTransitionPanel.raycastTarget = true;
        StartCoroutine(LoadTrySceneAfterFade());
    }

    private IEnumerator LoadTrySceneAfterFade()
    {
        yield return FadeTryTransitionPanel(0f, 1f);
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

    private IEnumerator FadeTryTransitionPanel(float startAlpha, float endAlpha)
    {
        if (tryTransitionPanel == null)
        {
            yield break;
        }

        float duration = Mathf.Max(0.01f, tryTransitionFadeTime);
        float timer = 0f;
        SetTryTransitionPanelAlpha(startAlpha);

        while (timer < duration)
        {
            timer += Time.unscaledDeltaTime;
            SetTryTransitionPanelAlpha(
                Mathf.Lerp(startAlpha, endAlpha, timer / duration)
            );
            yield return null;
        }

        SetTryTransitionPanelAlpha(endAlpha);
    }

    private void SetTryTransitionPanelAlpha(float alpha)
    {
        if (tryTransitionPanel == null)
        {
            return;
        }

        Color color = tryTransitionPanel.color;
        color.a = Mathf.Clamp01(alpha);
        tryTransitionPanel.color = color;
    }
}
