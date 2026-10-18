#include <iostream>

int main() {
    int* scores = new int[5]{90, 85, 77, 68, 95};
    int best = 0;
    for (int i = 0; i < 5; ++i) {
        if (scores[i] > best) {
            best = scores[i];
        }
    }
    std::cout << "best: " << best << "\n";
    delete scores;
    return 0;
}
