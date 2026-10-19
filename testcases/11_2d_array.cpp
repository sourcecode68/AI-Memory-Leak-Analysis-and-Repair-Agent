#include <iostream>
#include <stdlib.h>
int main()
{
    int **arr = static_cast<int **>(malloc(5 * sizeof(*arr)));

    for (int i = 0; i <= 4; i++)
    {
        arr[i] = static_cast<int *>(malloc(5 * sizeof(int)));
        for (int j = 0; j <= 4; j++)
        {
            arr[i][j] = i * 5 + j;
        }
    }

    int sum = 0;
    for (int i = 0; i <= 4; i++)
    {
        for (int j = 0; j <= 4; j++)
        {
            sum += arr[i][j];
        }
    }
    std::cout << "sum: " << sum << "\n";
    free(arr);
}
