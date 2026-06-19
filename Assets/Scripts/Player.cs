using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class Player : MonoBehaviour
{    
    public float moveDistance;
    private void Update()
    {
        Move();
    }

    private void Move()
    {
        Vector3 dir = Vector3.zero;

        if (Input.GetKeyDown(KeyCode.W))
        {
            dir = Vector3.up;
        }

        if (Input.GetKeyDown(KeyCode.S))
        {
            dir = Vector3.down;
        }

        if (Input.GetKeyDown(KeyCode.A))
        {
            dir = Vector3.left;
        }

        if (Input.GetKeyDown(KeyCode.D))
        {
            dir = Vector3.right;
        }

        if (dir != Vector3.zero)
        {
            Vector3 nextPos = transform.position + dir;

            Collider2D hit = Physics2D.OverlapPoint(nextPos);

            if (hit == null)
            {
                transform.position = nextPos;
            }
            else if (!hit.CompareTag("Wall") && !hit.CompareTag("Water"))
            {
                transform.position = nextPos;
            }
        }
    }
}
