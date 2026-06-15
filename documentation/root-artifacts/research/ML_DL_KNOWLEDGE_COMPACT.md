# Machine Learning & Deep Learning - Compacted Knowledge Context

**Last Updated**: 2026-06-10

---

## Core Learning Paradigms

### 1. Supervised Learning
- **Purpose**: Learn from labeled data (input-output pairs)
- **Types**: 
  - Classification: Discrete labels (spam/not spam)
  - Regression: Continuous values (price prediction)
- **Algorithms**: Linear/Logistic Regression, Decision Trees, Random Forests, SVM, Neural Networks

### 2. Unsupervised Learning
- **Purpose**: Discover patterns in unlabeled data
- **Types**:
  - Clustering: K-means, DBSCAN, Hierarchical
  - Association: Apriori algorithm
  - Dimensionality Reduction: PCA, t-SNE
- **Applications**: Customer segmentation, anomaly detection, recommendation systems

### 3. Reinforcement Learning
- **Purpose**: Agent learns optimal actions via trial-and-error
- **Core**: Agent, Environment, States, Actions, Rewards, Policy
- **Algorithms**: Q-Learning, Deep Q-Networks, Policy Gradients
- **Applications**: Game AI, robotics, autonomous vehicles

### 4. Self-Supervised Learning
- **Purpose**: Learn from unlabeled data by creating pseudo-labels
- **Techniques**: Contrastive learning, masked modeling (BERT), autoencoders
- **Advantage**: Reduces dependency on labeled data

---

## Deep Learning Essentials

### Architectures
- **CNNs**: Convolutional Neural Networks for computer vision
- **RNNs/LSTMs/GRUs**: Sequential data, time series, NLP
- **GANs**: Generator vs Discriminator for data generation
- **Transformers**: Attention mechanisms for NLP (covered in gaps)

### Core Components
- **Layers**: Input, Hidden, Output
- **Neurons**: Basic computational units
- **Weights & Biases**: Learnable parameters
- **Training**: Backpropagation using chain rule

### Generative Adversarial Networks (GANs)
- **Structure**: Generator creates fake data, Discriminator distinguishes real/fake
- **Types**: Vanilla GAN, DCGAN, StyleGAN, CycleGAN
- **Applications**: Image generation, data augmentation, art creation

---

## Mathematical Foundations

### Linear Algebra
- **Basics**: Vectors, matrices, dot product, matrix multiplication
- **Advanced**: Eigenvalues/eigenvectors, SVD, matrix factorization
- **ML Use**: Data representation, PCA, neural network computations

### Calculus for Optimization
- **Derivatives**: Rate of change, finding minima/maxima
- **Gradients**: Direction of steepest ascent in multivariable functions
- **Chain Rule**: Essential for backpropagation
- **Purpose**: Optimize loss functions via gradient descent

### Probability & Statistics
- **Probability**: Event likelihood, distributions (Normal, Binomial, Poisson)
- **Statistics**: Mean, variance, hypothesis testing, confidence intervals
- **Bayesian Inference**: P(θ|data) ∝ P(data|θ) × P(θ)
- **ML Use**: Model evaluation, uncertainty estimation

### Time Series Analysis
- **Components**: Trend, seasonality, cyclical patterns, noise
- **Models**: ARIMA, SARIMA, Exponential Smoothing, LSTMs
- **Applications**: Forecasting (stock prices, weather, sales)

---

## Model Development

### Optimization Techniques
**First-order (gradient-based)**:
- Batch/Mini-batch/Stochastic Gradient Descent (SGD)
- Momentum, RMSProp, Adam, AdaGrad

**Second-order**:
- Newton's Method, BFGS, Quasi-Newton

**Metaheuristics**:
- Genetic Algorithms, Particle Swarm, Simulated Annealing

**Purpose**: Minimize loss functions to improve accuracy

### Regularization Techniques
**Purpose**: Prevent overfitting
- **L1 (Lasso)**: Absolute penalty, promotes sparsity, feature selection
- **L2 (Ridge)**: Squared penalty, shrinks coefficients
- **Elastic Net**: L1 + L2 combination
- **Others**: Dropout, Early Stopping, Data Augmentation

### Loss Functions
**Principle**: Choose loss aligned with target functional (mean, median, quantile)

**Regression**:
- MSE (Mean Squared Error): Penalizes large errors
- MAE (Mean Absolute Error): Robust to outliers
- Huber Loss: Hybrid MSE+MAE
- Poisson/Gamma Deviance: For specific distributions

**Classification**:
- Cross-Entropy: Standard for classification
- Hinge Loss: SVM margin-based
- Brier Score: Probabilistic predictions

### Activation Functions
**Purpose**: Introduce non-linearity in neural networks

| Function | Formula | Range | Use Case |
|----------|---------|-------|----------|
| ReLU | max(0, x) | [0, ∞) | Hidden layers (most common) |
| Leaky ReLU | max(αx, x) | (-∞, ∞) | Fixes dying ReLU |
| Sigmoid | 1/(1+e^-x) | (0, 1) | Binary classification output |
| Tanh | (e^x - e^-x)/(e^x + e^-x) | (-1, 1) | Hidden layers (zero-centered) |
| Softmax | e^xi / Σe^xj | (0, 1) sum=1 | Multi-class output |
| ELU | x if x>0, α(e^x-1) | (-α, ∞) | Alternative to ReLU |

---

## Practical Implementation

### Data Preprocessing & Feature Engineering
**Preprocessing**:
- Handle missing values (imputation/removal)
- Normalization/Standardization
- Encode categorical variables (one-hot, label encoding)

**Feature Engineering**:
- Create new features from existing ones
- Feature selection (remove irrelevant features)
- Binning, text vectorization, feature splitting
- **Impact**: Directly affects model performance

### Model Evaluation & Metrics
**Classification**:
- Accuracy, Precision, Recall, F1-Score
- ROC-AUC, Confusion Matrix
- Log Loss (probabilistic)

**Regression**:
- MSE, RMSE, MAE
- R² (coefficient of determination)
- MAPE (Mean Absolute Percentage Error)

**Principle**: Choose metrics aligned with business goals; consider class imbalance

### Hyperparameter Tuning
**Methods**:
- **Grid Search**: Exhaustive search over parameter grid (slow, thorough)
- **Random Search**: Random sampling (faster, often sufficient)
- **Bayesian Optimization**: Smart search using probabilistic models

**Common Hyperparameters**:
- Learning rate, batch size, epochs
- Tree depth, number of estimators
- Regularization strength (λ, α)
- Dropout rate

---

## Knowledge Gaps Requiring Further Research

### High Priority
1. **Data Preprocessing Specifics**:
   - Handling imbalanced datasets (SMOTE, class weights)
   - Missing data imputation methods
   - Outlier detection techniques

2. **Model Interpretation & Explainability**:
   - SHAP values
   - LIME
   - Feature importance methods
   - Attention mechanisms

3. **Advanced Optimization**:
   - Learning rate scheduling strategies
   - Adaptive optimizers comparison (Adam vs AdamW vs AdaBound)
   - Convergence guarantees

4. **Cross-Validation Strategies**:
   - K-Fold, Stratified, Time Series CV
   - Train/Val/Test split best practices

### Medium Priority
5. **Transfer Learning**: Pre-trained models, fine-tuning strategies
6. **Ensemble Methods**: Bagging, Boosting (XGBoost, LightGBM, CatBoost)
7. **Neural Architecture Search (NAS)**
8. **Batch Normalization vs Layer Normalization**
9. **Gradient Issues**: Vanishing/exploding gradients solutions

### Domain-Specific
10. **Computer Vision**: Object detection (YOLO, R-CNN), Image segmentation
11. **NLP**: Transformers, BERT, GPT architecture details, Tokenization
12. **Graph Neural Networks (GNNs)**

---

## Status
- ✅ Core concepts documented
- ✅ Mathematical foundations covered
- ✅ Practical implementation essentials captured
- ⏳ 12 advanced topics identified for deeper research

**Next Steps**: Address high-priority gaps (1-4) before moving to domain-specific topics
