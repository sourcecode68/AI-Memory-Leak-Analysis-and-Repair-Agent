#include <iostream>
#include <memory>
#include <vector>

struct Node {
    int value = 0;
    std::unique_ptr<Node> next;
};

int main() {
    std::unique_ptr<Node> head;
    for (int i = 3; i >= 1; --i) {
        auto node = std::make_unique<Node>();
        node->value = i;
        node->next = std::move(head);
        head = std::move(node);
    }
    int sum = 0;
    for (Node* n = head.get(); n != nullptr; n = n->next.get()) {
        sum += n->value;
    }

    std::vector<int> squares;
    for (int i = 1; i <= 4; ++i) {
        squares.push_back(i * i);
    }

    int* answer = new int(42);
    std::cout << "list sum: " << sum << ", squares: " << squares.size()
              << ", answer: " << *answer << "\n";
    delete answer;
    return 0;
}
